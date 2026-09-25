"""持久化边界：SQLite 结构定义与仓储方法。

所有写操作都在显式事务中完成（BEGIN IMMEDIATE），时间一律以 UTC ISO
字符串存储，列表/字典字段以 JSON 存储。历史表（mapping_decisions、
approvals、audit_events）只插入不更新，保证重启后与运行中一致、且全程可追溯。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS actors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    roles TEXT NOT NULL,
    parties TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS standards (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    country TEXT NOT NULL,
    issuing_body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS standard_versions (
    id TEXT PRIMARY KEY,
    standard_id TEXT NOT NULL REFERENCES standards(id),
    version_label TEXT NOT NULL,
    parent_version_id TEXT REFERENCES standard_versions(id),
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    effective_from TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    retired_at TEXT,
    UNIQUE (standard_id, version_label)
);
CREATE TABLE IF NOT EXISTS competency_units (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES standard_versions(id),
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    hours INTEGER NOT NULL CHECK (hours >= 0),
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
    practical_scope TEXT NOT NULL,
    UNIQUE (version_id, code)
);
CREATE TABLE IF NOT EXISTS evidence_requirements (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES competency_units(id),
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    mandatory INTEGER NOT NULL,
    UNIQUE (unit_id, kind)
);
CREATE TABLE IF NOT EXISTS mappings (
    id TEXT PRIMARY KEY,
    unit_a_id TEXT NOT NULL REFERENCES competency_units(id),
    unit_b_id TEXT NOT NULL REFERENCES competency_units(id),
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(id),
    created_at TEXT NOT NULL,
    UNIQUE (unit_a_id, unit_b_id)
);
CREATE TABLE IF NOT EXISTS mapping_decisions (
    id TEXT PRIMARY KEY,
    mapping_id TEXT NOT NULL REFERENCES mappings(id),
    seq INTEGER NOT NULL,
    direction TEXT NOT NULL,
    outcome TEXT NOT NULL,
    conditions TEXT NOT NULL,
    source TEXT NOT NULL,
    rationale TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (mapping_id, direction, seq)
);
CREATE TABLE IF NOT EXISTS exception_cases (
    id TEXT PRIMARY KEY,
    mapping_id TEXT NOT NULL REFERENCES mappings(id),
    direction TEXT NOT NULL,
    effect TEXT NOT NULL,
    conditions TEXT NOT NULL,
    reason TEXT NOT NULL,
    required_parties TEXT NOT NULL,
    proposed_by TEXT NOT NULL REFERENCES actors(id),
    status TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exception_cases(id),
    party TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(id),
    decision TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (exception_id, party),
    UNIQUE (exception_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_version ON competency_units(version_id);
CREATE INDEX IF NOT EXISTS idx_evidence_unit ON evidence_requirements(unit_id);
CREATE INDEX IF NOT EXISTS idx_decisions_mapping ON mapping_decisions(mapping_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_mapping ON exception_cases(mapping_id);
CREATE INDEX IF NOT EXISTS idx_approvals_exception ON approvals(exception_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id);
"""

# 需要 JSON 编解码的列：表 -> 列集合
_JSON_COLUMNS = {
    "actors": {"roles", "parties"},
    "competency_units": {"practical_scope"},
    "mapping_decisions": {"conditions"},
    "exception_cases": {"conditions", "required_parties"},
    "audit_events": {"payload"},
}


def _decode(table: str, row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    for col in _JSON_COLUMNS.get(table, ()):
        if col in row and isinstance(row[col], str):
            row[col] = json.loads(row[col])
    if table == "evidence_requirements" and "mandatory" in row:
        row["mandatory"] = bool(row["mandatory"])
    return row


class Database:
    """SQLite 连接与事务管理；单连接 + 可重入锁，供多线程 HTTP 层共享。"""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._closed = False

    @contextmanager
    def transaction(self):
        """显式事务：期间所有 execute 同属一个原子单元，异常即回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        """关闭连接并截断 WAL；幂等，重复调用安全。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._conn.close()


class Repository:
    """仓储：SQL 与 JSON 编解码集中于此，向上只暴露 dict。"""

    def __init__(self, db: Database):
        self._db = db

    # ---- actors ----
    def insert_actor(self, *, id, name, token_hash, roles, parties, created_at) -> None:
        self._db.execute(
            "INSERT INTO actors (id, name, token_hash, roles, parties, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (id, name, token_hash, json.dumps(roles, ensure_ascii=False),
             json.dumps(parties, ensure_ascii=False), created_at),
        )

    def get_actor(self, actor_id: str) -> Optional[dict]:
        return _decode("actors", self._db.query_one("SELECT * FROM actors WHERE id=?", (actor_id,)))

    def get_actor_by_name(self, name: str) -> Optional[dict]:
        return _decode("actors", self._db.query_one("SELECT * FROM actors WHERE name=?", (name,)))

    def get_actor_by_token_hash(self, token_hash: str) -> Optional[dict]:
        return _decode("actors", self._db.query_one("SELECT * FROM actors WHERE token_hash=?", (token_hash,)))

    # ---- standards ----
    def insert_standard(self, *, id, code, title, country, issuing_body, created_at) -> None:
        self._db.execute(
            "INSERT INTO standards (id, code, title, country, issuing_body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (id, code, title, country, issuing_body, created_at),
        )

    def get_standard(self, standard_id: str) -> Optional[dict]:
        return self._db.query_one("SELECT * FROM standards WHERE id=?", (standard_id,))

    def get_standard_by_code(self, code: str) -> Optional[dict]:
        return self._db.query_one("SELECT * FROM standards WHERE code=?", (code,))

    # ---- versions ----
    def insert_version(self, *, id, standard_id, version_label, parent_version_id,
                       status, content_hash, effective_from, created_at) -> None:
        self._db.execute(
            "INSERT INTO standard_versions"
            " (id, standard_id, version_label, parent_version_id, status, content_hash,"
            "  effective_from, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, standard_id, version_label, parent_version_id, status, content_hash,
             effective_from, created_at),
        )

    def get_version(self, version_id: str) -> Optional[dict]:
        return self._db.query_one("SELECT * FROM standard_versions WHERE id=?", (version_id,))

    def get_version_by_label(self, standard_id: str, label: str) -> Optional[dict]:
        return self._db.query_one(
            "SELECT * FROM standard_versions WHERE standard_id=? AND version_label=?",
            (standard_id, label),
        )

    def list_versions(self, standard_id: str) -> list[dict]:
        return self._db.query(
            "SELECT * FROM standard_versions WHERE standard_id=? ORDER BY created_at, id",
            (standard_id,),
        )

    def update_version_status(self, version_id: str, status: str, *,
                              published_at=None, retired_at=None) -> None:
        self._db.execute(
            "UPDATE standard_versions SET status=?,"
            " published_at=COALESCE(?, published_at),"
            " retired_at=COALESCE(?, retired_at) WHERE id=?",
            (status, published_at, retired_at, version_id),
        )

    # ---- units & evidence ----
    def insert_unit(self, *, id, version_id, code, title, hours, level, practical_scope) -> None:
        self._db.execute(
            "INSERT INTO competency_units"
            " (id, version_id, code, title, hours, level, practical_scope)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, version_id, code, title, hours, level,
             json.dumps(practical_scope, ensure_ascii=False)),
        )

    def get_unit(self, unit_id: str) -> Optional[dict]:
        return _decode("competency_units",
                       self._db.query_one("SELECT * FROM competency_units WHERE id=?", (unit_id,)))

    def get_unit_context(self, unit_id: str) -> Optional[dict]:
        """能力单元连同所属版本与标准的上下文。"""
        row = self._db.query_one(
            "SELECT u.id, u.version_id, u.code, u.title, u.hours, u.level, u.practical_scope,"
            " v.version_label, v.status AS version_status, v.standard_id,"
            " s.code AS standard_code, s.title AS standard_title, s.country AS standard_country"
            " FROM competency_units u"
            " JOIN standard_versions v ON v.id = u.version_id"
            " JOIN standards s ON s.id = v.standard_id"
            " WHERE u.id=?",
            (unit_id,),
        )
        return _decode("competency_units", row)

    def list_units(self, version_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM competency_units WHERE version_id=? ORDER BY code", (version_id,))
        return [_decode("competency_units", r) for r in rows]

    def insert_evidence(self, *, id, unit_id, kind, detail, mandatory) -> None:
        self._db.execute(
            "INSERT INTO evidence_requirements (id, unit_id, kind, detail, mandatory)"
            " VALUES (?, ?, ?, ?, ?)",
            (id, unit_id, kind, detail, 1 if mandatory else 0),
        )

    def list_evidence(self, unit_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM evidence_requirements WHERE unit_id=? ORDER BY kind", (unit_id,))
        return [_decode("evidence_requirements", r) for r in rows]

    # ---- mappings ----
    def insert_mapping(self, *, id, unit_a_id, unit_b_id, status, created_by, created_at) -> None:
        self._db.execute(
            "INSERT INTO mappings (id, unit_a_id, unit_b_id, status, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (id, unit_a_id, unit_b_id, status, created_by, created_at),
        )

    def get_mapping(self, mapping_id: str) -> Optional[dict]:
        return self._db.query_one("SELECT * FROM mappings WHERE id=?", (mapping_id,))

    def find_mapping(self, unit_a_id: str, unit_b_id: str) -> Optional[dict]:
        return self._db.query_one(
            "SELECT * FROM mappings WHERE unit_a_id=? AND unit_b_id=?",
            (unit_a_id, unit_b_id),
        )

    def update_mapping_status(self, mapping_id: str, status: str) -> None:
        self._db.execute("UPDATE mappings SET status=? WHERE id=?", (status, mapping_id))

    def list_mappings_touching_versions(self, version_ids: list[str], status: str) -> list[dict]:
        if not version_ids:
            return []
        ph = ",".join("?" for _ in version_ids)
        return self._db.query(
            f"SELECT DISTINCT m.* FROM mappings m"
            f" JOIN competency_units ua ON ua.id = m.unit_a_id"
            f" JOIN competency_units ub ON ub.id = m.unit_b_id"
            f" WHERE m.status=? AND (ua.version_id IN ({ph}) OR ub.version_id IN ({ph}))",
            [status, *version_ids, *version_ids],
        )

    def list_mappings_of_unit(self, unit_id: str, status: Optional[str] = None) -> list[dict]:
        if status is None:
            return self._db.query(
                "SELECT * FROM mappings WHERE unit_a_id=? OR unit_b_id=?", (unit_id, unit_id))
        return self._db.query(
            "SELECT * FROM mappings WHERE status=? AND (unit_a_id=? OR unit_b_id=?)",
            (status, unit_id, unit_id),
        )

    # ---- decisions（只增不改的历史） ----
    def insert_decision(self, *, id, mapping_id, seq, direction, outcome, conditions,
                        source, rationale, decided_at) -> None:
        self._db.execute(
            "INSERT INTO mapping_decisions"
            " (id, mapping_id, seq, direction, outcome, conditions, source, rationale, decided_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, mapping_id, seq, direction, outcome,
             json.dumps(conditions, ensure_ascii=False), source, rationale, decided_at),
        )

    def list_decisions(self, mapping_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM mapping_decisions WHERE mapping_id=? ORDER BY seq, direction",
            (mapping_id,),
        )
        return [_decode("mapping_decisions", r) for r in rows]

    def latest_decision(self, mapping_id: str, direction: str) -> Optional[dict]:
        return _decode("mapping_decisions", self._db.query_one(
            "SELECT * FROM mapping_decisions WHERE mapping_id=? AND direction=?"
            " ORDER BY seq DESC LIMIT 1",
            (mapping_id, direction),
        ))

    def max_decision_seq(self, mapping_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM mapping_decisions WHERE mapping_id=?",
            (mapping_id,),
        )
        return int(row["s"])

    # ---- exceptions ----
    def insert_exception(self, *, id, mapping_id, direction, effect, conditions, reason,
                         required_parties, proposed_by, status, valid_from, valid_until,
                         created_at) -> None:
        self._db.execute(
            "INSERT INTO exception_cases"
            " (id, mapping_id, direction, effect, conditions, reason, required_parties,"
            "  proposed_by, status, valid_from, valid_until, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, mapping_id, direction, effect,
             json.dumps(conditions, ensure_ascii=False), reason,
             json.dumps(required_parties, ensure_ascii=False), proposed_by, status,
             valid_from, valid_until, created_at),
        )

    def get_exception(self, exception_id: str) -> Optional[dict]:
        return _decode("exception_cases",
                       self._db.query_one("SELECT * FROM exception_cases WHERE id=?", (exception_id,)))

    def list_exceptions(self, mapping_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM exception_cases WHERE mapping_id=? ORDER BY created_at, id",
            (mapping_id,),
        )
        return [_decode("exception_cases", r) for r in rows]

    def update_exception_status(self, exception_id: str, status: str, *,
                                decided_at=None) -> None:
        self._db.execute(
            "UPDATE exception_cases SET status=?,"
            " decided_at=COALESCE(?, decided_at) WHERE id=?",
            (status, decided_at, exception_id),
        )

    def find_active_exception(self, mapping_id: str, direction: str, at: str) -> Optional[dict]:
        """指定瞬时对某方向生效的已批准例外；重叠时取最近决定者。"""
        return _decode("exception_cases", self._db.query_one(
            "SELECT * FROM exception_cases"
            " WHERE mapping_id=? AND status='approved'"
            " AND (direction=? OR direction='BOTH')"
            " AND valid_from<=? AND valid_until>?"
            " ORDER BY decided_at DESC, id DESC LIMIT 1",
            (mapping_id, direction, at, at),
        ))

    # ---- approvals（会签，只增不改） ----
    def insert_approval(self, *, id, exception_id, party, actor_id, decision,
                        idempotency_key, created_at) -> None:
        self._db.execute(
            "INSERT INTO approvals"
            " (id, exception_id, party, actor_id, decision, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, exception_id, party, actor_id, decision, idempotency_key, created_at),
        )

    def get_approval_by_key(self, exception_id: str, idempotency_key: str) -> Optional[dict]:
        return self._db.query_one(
            "SELECT * FROM approvals WHERE exception_id=? AND idempotency_key=?",
            (exception_id, idempotency_key),
        )

    def get_approval_by_party(self, exception_id: str, party: str) -> Optional[dict]:
        return self._db.query_one(
            "SELECT * FROM approvals WHERE exception_id=? AND party=?",
            (exception_id, party),
        )

    def list_approvals(self, exception_id: str) -> list[dict]:
        return self._db.query(
            "SELECT * FROM approvals WHERE exception_id=? ORDER BY created_at, id",
            (exception_id,),
        )

    # ---- audit ----
    def insert_event(self, *, id, entity_type, entity_id, action, actor_id, payload, created_at) -> None:
        self._db.execute(
            "INSERT INTO audit_events"
            " (id, entity_type, entity_id, action, actor_id, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, entity_type, entity_id, action, actor_id,
             json.dumps(payload, ensure_ascii=False), created_at),
        )

    def list_events(self, entity_type: str, entity_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=?"
            " ORDER BY created_at, id",
            (entity_type, entity_id),
        )
        return [_decode("audit_events", r) for r in rows]
