"""SQLite 持久化：模式定义、连接管理与数据访问。

一致性约定：
- WAL 日志 + 外键强制 + busy_timeout，服务重启后已提交数据完整可见；
- 所有多步写入包在显式事务（BEGIN IMMEDIATE）中，失败整体回滚；
- 历史决定（decisions）与审计事件（audit_events）只插入、不更新、不删除。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS standards (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    country     TEXT NOT NULL,
    authority   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standard_versions (
    id                 TEXT PRIMARY KEY,
    standard_id        TEXT NOT NULL REFERENCES standards(id),
    version_label      TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('draft','published','superseded')),
    parent_version_id  TEXT REFERENCES standard_versions(id),
    published_at       TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (standard_id, version_label)
);

CREATE TABLE IF NOT EXISTS capability_units (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES standard_versions(id),
    code            TEXT NOT NULL,
    title           TEXT NOT NULL,
    level           INTEGER NOT NULL CHECK (level >= 1),
    credit_hours    REAL NOT NULL CHECK (credit_hours > 0),
    practical_scope TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    UNIQUE (version_id, code)
);
CREATE INDEX IF NOT EXISTS ix_units_version ON capability_units(version_id);

CREATE TABLE IF NOT EXISTS evidence_requirements (
    id          TEXT PRIMARY KEY,
    unit_id     TEXT NOT NULL REFERENCES capability_units(id),
    kind        TEXT NOT NULL,
    description TEXT NOT NULL,
    mandatory   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_unit ON evidence_requirements(unit_id);

CREATE TABLE IF NOT EXISTS mappings (
    id                 TEXT PRIMARY KEY,
    source_unit_id     TEXT NOT NULL REFERENCES capability_units(id),
    target_unit_id     TEXT NOT NULL REFERENCES capability_units(id),
    rationale          TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL
                       CHECK (status IN ('proposed','approved','under_review','revoked')),
    affected_by_update INTEGER NOT NULL DEFAULT 0,
    approval_round     INTEGER NOT NULL DEFAULT 0,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    revoked_at         TEXT,
    revoke_reason      TEXT,
    UNIQUE (source_unit_id, target_unit_id)
);
CREATE INDEX IF NOT EXISTS ix_mappings_source ON mappings(source_unit_id);
CREATE INDEX IF NOT EXISTS ix_mappings_target ON mappings(target_unit_id);

-- 历史决定：只允许 INSERT，任何更新需求都以新行表达。
CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    mapping_id      TEXT NOT NULL REFERENCES mappings(id),
    from_version_id TEXT NOT NULL REFERENCES standard_versions(id),
    to_version_id   TEXT NOT NULL REFERENCES standard_versions(id),
    outcome         TEXT NOT NULL CHECK (outcome IN ('full','conditional','none')),
    conditions      TEXT NOT NULL DEFAULT '[]',
    details         TEXT NOT NULL DEFAULT '{}',
    exception_codes TEXT NOT NULL DEFAULT '[]',
    rule_version    TEXT NOT NULL,
    computed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_mapping ON decisions(mapping_id);

CREATE TABLE IF NOT EXISTS exceptions (
    id                     TEXT PRIMARY KEY,
    code                   TEXT NOT NULL UNIQUE,
    version_id             TEXT NOT NULL REFERENCES standard_versions(id),
    mapping_id             TEXT REFERENCES mappings(id),
    kind                   TEXT NOT NULL
                           CHECK (kind IN ('force_full','waive_hours','waive_scope','waive_level')),
    reason                 TEXT NOT NULL,
    proposed_by            TEXT NOT NULL,
    counterparty_authority TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('proposed','approved','revoked')),
    approval_round         INTEGER NOT NULL DEFAULT 0,
    effective_from         TEXT NOT NULL,
    effective_until        TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    revoked_at             TEXT
);

-- 会签：每个机构在每一轮对每个对象最多一票，唯一约束即幂等约束。
-- 映射被标记待复核后轮次 +1，双方需在新一轮重新会签。
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('mapping','exception')),
    subject_id   TEXT NOT NULL,
    party        TEXT NOT NULL,
    actor        TEXT NOT NULL,
    round        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (subject_type, subject_id, party, round)
);
CREATE INDEX IF NOT EXISTS ix_approvals_subject ON approvals(subject_type, subject_id);

-- 幂等键：同一键的重复请求直接重放首次响应。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    actor      TEXT NOT NULL,
    endpoint   TEXT NOT NULL,
    response   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 审计事件：只追加，供追溯。
CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_events(entity_type, entity_id);
"""


def _scope_to_text(scope: Any) -> str:
    return json.dumps(sorted(scope), ensure_ascii=False)


class Database:
    """SQLite 连接与数据访问。单连接 + 可重入锁，线程安全。"""

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：块内所有写入要么全部提交，要么整体回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # 标准与版本
    # ------------------------------------------------------------------
    def insert_standard(self, conn, *, id, code, title, country, authority, created_at):
        conn.execute(
            "INSERT INTO standards (id, code, title, country, authority, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (id, code, title, country, authority, created_at),
        )

    def find_standard_by_code(self, code: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM standards WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None

    def get_standard(self, standard_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM standards WHERE id = ?", (standard_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_version(self, conn, *, id, standard_id, version_label, status,
                       parent_version_id, published_at, created_at):
        conn.execute(
            "INSERT INTO standard_versions"
            " (id, standard_id, version_label, status, parent_version_id, published_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, standard_id, version_label, status, parent_version_id, published_at, created_at),
        )

    def get_version(self, version_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM standard_versions WHERE id = ?", (version_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_version(self, standard_id: str, label: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM standard_versions WHERE standard_id = ? AND version_label = ?",
            (standard_id, label),
        ).fetchone()
        return dict(row) if row else None

    def list_versions(self, standard_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM standard_versions WHERE standard_id = ? ORDER BY rowid",
            (standard_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_version_status(self, conn, version_id: str, status: str) -> None:
        conn.execute(
            "UPDATE standard_versions SET status = ? WHERE id = ?", (status, version_id)
        )

    # ------------------------------------------------------------------
    # 能力单元与证据要求
    # ------------------------------------------------------------------
    def insert_unit(self, conn, *, id, version_id, code, title, level,
                    credit_hours, practical_scope, created_at):
        conn.execute(
            "INSERT INTO capability_units"
            " (id, version_id, code, title, level, credit_hours, practical_scope, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, version_id, code, title, level, credit_hours,
             _scope_to_text(practical_scope), created_at),
        )

    def get_unit(self, unit_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM capability_units WHERE id = ?", (unit_id,)
        ).fetchone()
        return self._unit_dict(row) if row else None

    def list_units(self, version_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM capability_units WHERE version_id = ? ORDER BY code",
            (version_id,),
        ).fetchall()
        return [self._unit_dict(r) for r in rows]

    @staticmethod
    def _unit_dict(row) -> dict:
        data = dict(row)
        data["practical_scope"] = sorted(json.loads(data["practical_scope"]))
        return data

    def insert_evidence(self, conn, *, id, unit_id, kind, description, mandatory, created_at):
        conn.execute(
            "INSERT INTO evidence_requirements (id, unit_id, kind, description, mandatory, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (id, unit_id, kind, description, int(bool(mandatory)), created_at),
        )

    def list_evidence(self, unit_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM evidence_requirements WHERE unit_id = ? ORDER BY kind, id",
            (unit_id,),
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["mandatory"] = bool(item["mandatory"])
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # 映射
    # ------------------------------------------------------------------
    def insert_mapping(self, conn, *, id, source_unit_id, target_unit_id, rationale,
                       status, created_by, created_at):
        conn.execute(
            "INSERT INTO mappings"
            " (id, source_unit_id, target_unit_id, rationale, status,"
            "  affected_by_update, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (id, source_unit_id, target_unit_id, rationale, status, created_by, created_at),
        )

    def get_mapping(self, mapping_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        return self._mapping_dict(row) if row else None

    def find_mapping(self, source_unit_id: str, target_unit_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mappings WHERE source_unit_id = ? AND target_unit_id = ?",
            (source_unit_id, target_unit_id),
        ).fetchone()
        return self._mapping_dict(row) if row else None

    def list_mappings_for_unit(self, unit_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mappings WHERE source_unit_id = ? OR target_unit_id = ?"
            " ORDER BY rowid",
            (unit_id, unit_id),
        ).fetchall()
        return [self._mapping_dict(r) for r in rows]

    def list_mappings_between_versions(self, version_a: str, version_b: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT m.* FROM mappings m
            JOIN capability_units su ON su.id = m.source_unit_id
            JOIN capability_units tu ON tu.id = m.target_unit_id
            WHERE (su.version_id = ? AND tu.version_id = ?)
               OR (su.version_id = ? AND tu.version_id = ?)
            ORDER BY m.rowid
            """,
            (version_a, version_b, version_b, version_a),
        ).fetchall()
        return [self._mapping_dict(r) for r in rows]

    def list_mappings_touching_versions(self, version_ids: list[str]) -> list[dict]:
        if not version_ids:
            return []
        marks = ",".join("?" for _ in version_ids)
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT m.* FROM mappings m
            JOIN capability_units su ON su.id = m.source_unit_id
            JOIN capability_units tu ON tu.id = m.target_unit_id
            WHERE su.version_id IN ({marks}) OR tu.version_id IN ({marks})
            ORDER BY m.rowid
            """,
            (*version_ids, *version_ids),
        ).fetchall()
        return [self._mapping_dict(r) for r in rows]

    def update_mapping_status(self, conn, mapping_id: str, status: str,
                              revoked_at=None, revoke_reason=None) -> None:
        conn.execute(
            "UPDATE mappings SET status = ?, revoked_at = COALESCE(?, revoked_at),"
            " revoke_reason = COALESCE(?, revoke_reason) WHERE id = ?",
            (status, revoked_at, revoke_reason, mapping_id),
        )

    def mark_mapping_affected(self, conn, mapping_id: str, under_review: bool) -> None:
        if under_review:
            conn.execute(
                "UPDATE mappings SET affected_by_update = 1, status ="
                " CASE WHEN status = 'approved' THEN 'under_review' ELSE status END"
                " WHERE id = ?",
                (mapping_id,),
            )
        else:
            conn.execute(
                "UPDATE mappings SET affected_by_update = 1 WHERE id = ?", (mapping_id,)
            )

    @staticmethod
    def _mapping_dict(row) -> dict:
        data = dict(row)
        data["affected_by_update"] = bool(data["affected_by_update"])
        return data

    # ------------------------------------------------------------------
    # 决定（只追加）
    # ------------------------------------------------------------------
    def insert_decision(self, conn, *, id, mapping_id, from_version_id, to_version_id,
                        outcome, conditions, details, exception_codes,
                        rule_version, computed_at):
        conn.execute(
            "INSERT INTO decisions"
            " (id, mapping_id, from_version_id, to_version_id, outcome, conditions,"
            "  details, exception_codes, rule_version, computed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, mapping_id, from_version_id, to_version_id, outcome,
             json.dumps(list(conditions), ensure_ascii=False),
             json.dumps(details, ensure_ascii=False),
             json.dumps(list(exception_codes), ensure_ascii=False),
             rule_version, computed_at),
        )

    def list_decisions(self, mapping_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE mapping_id = ? ORDER BY rowid",
            (mapping_id,),
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["conditions"] = json.loads(item["conditions"])
            item["details"] = json.loads(item["details"])
            item["exception_codes"] = json.loads(item["exception_codes"])
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # 例外
    # ------------------------------------------------------------------
    def insert_exception(self, conn, *, id, code, version_id, mapping_id, kind, reason,
                         proposed_by, counterparty_authority, status,
                         effective_from, effective_until, created_at):
        conn.execute(
            "INSERT INTO exceptions"
            " (id, code, version_id, mapping_id, kind, reason, proposed_by,"
            "  counterparty_authority, status, effective_from, effective_until, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, code, version_id, mapping_id, kind, reason, proposed_by,
             counterparty_authority, status, effective_from, effective_until, created_at),
        )

    def get_exception(self, exception_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM exceptions WHERE id = ?", (exception_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_exception_by_code(self, code: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM exceptions WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None

    def update_exception_status(self, conn, exception_id: str, status: str,
                                revoked_at=None) -> None:
        conn.execute(
            "UPDATE exceptions SET status = ?, revoked_at = COALESCE(?, revoked_at)"
            " WHERE id = ?",
            (status, revoked_at, exception_id),
        )

    def list_exceptions_for_mapping(self, mapping_id: str,
                                    version_ids: list[str]) -> list[dict]:
        """与某映射相关的例外：直接绑定该映射，或绑定其任一端版本。"""
        marks = ",".join("?" for _ in version_ids)
        rows = self._conn.execute(
            f"SELECT * FROM exceptions WHERE mapping_id = ? OR version_id IN ({marks})"
            " ORDER BY rowid",
            (mapping_id, *version_ids),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 会签
    # ------------------------------------------------------------------
    def insert_approval(self, conn, *, id, subject_type, subject_id, party, actor,
                        created_at, round=0):
        conn.execute(
            "INSERT INTO approvals (id, subject_type, subject_id, party, actor, round, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, subject_type, subject_id, party, actor, round, created_at),
        )

    def list_approvals(self, subject_type: str, subject_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE subject_type = ? AND subject_id = ?"
            " ORDER BY rowid",
            (subject_type, subject_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 幂等键
    # ------------------------------------------------------------------
    def find_idempotency(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def insert_idempotency(self, conn, *, key, actor, endpoint, response, created_at):
        conn.execute(
            "INSERT INTO idempotency_keys (key, actor, endpoint, response, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, actor, endpoint, response, created_at),
        )

    # ------------------------------------------------------------------
    # 审计事件（只追加）
    # ------------------------------------------------------------------
    def insert_audit(self, conn, *, id, entity_type, entity_id, action, actor,
                     detail, created_at):
        conn.execute(
            "INSERT INTO audit_events (id, entity_type, entity_id, action, actor, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, entity_type, entity_id, action, actor,
             json.dumps(detail, ensure_ascii=False), created_at),
        )

    def list_audit(self, entity_type: str, entity_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE entity_type = ? AND entity_id = ?"
            " ORDER BY rowid",
            (entity_type, entity_id),
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result
