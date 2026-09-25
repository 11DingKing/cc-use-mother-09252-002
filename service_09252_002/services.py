"""应用服务：导入、比对、会签、撤销、追溯与例外管理。

设计要点：
- 每个写操作在单个事务内完成，并把幂等键与响应一并提交，
  重复请求（网络重试 / 双击）得到与首次完全一致的结果；
- 会签要求映射双方机构各一票，且同一操作者不得代表两方；
- 历史决定只追加不修改：源标准更新仅把受影响映射标记为待复核；
- 当前时间只来自注入的时钟端口，便于复现跨时区与期限边界。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Callable

from .clock import Clock, SystemClock, format_instant, parse_instant
from .domain import (
    ActiveException,
    Actor,
    ExceptionKind,
    ExceptionStatus,
    MappingStatus,
    Role,
    SubjectType,
    UnitFacts,
    VersionStatus,
)
from .errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from .rules import RULE_VERSION, decide
from .storage import Database


def _uuid() -> str:
    return uuid.uuid4().hex


class RecognitionService:
    """职业技能标准互认的应用服务。"""

    def __init__(self, db: Database, clock: Clock | None = None,
                 id_generator: Callable[[], str] | None = None):
        self._db = db
        self._clock = clock or SystemClock()
        self._ids = id_generator or _uuid

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{self._ids()}"

    def _now(self) -> str:
        return format_instant(self._clock.now())

    @staticmethod
    def _require(actor: Actor, *roles: str) -> None:
        if not actor.has_any(*roles):
            raise PermissionDeniedError(
                f"操作者 {actor.name} 缺少角色，需要: {'/'.join(roles)}"
            )

    def _audit(self, conn, entity_type: str, entity_id: str, action: str,
               actor: str, detail: dict) -> None:
        self._db.insert_audit(
            conn, id=self._new_id("evt"), entity_type=entity_type, entity_id=entity_id,
            action=action, actor=actor, detail=detail, created_at=self._now(),
        )

    def _execute(self, actor: Actor, endpoint: str, idempotency_key: str | None,
                 work: Callable[[Any], dict]) -> dict:
        """在单事务中执行写操作，并落实幂等键语义。"""
        if idempotency_key:
            existing = self._db.find_idempotency(idempotency_key)
            if existing:
                if existing["actor"] != actor.name or existing["endpoint"] != endpoint:
                    raise ConflictError("幂等键已被其他请求占用")
                replay = json.loads(existing["response"])
                replay["meta"] = {**replay.get("meta", {}), "idempotent_replay": True}
                return replay
        try:
            with self._db.transaction() as conn:
                result = work(conn)
                result.setdefault("meta", {})["idempotent_replay"] = False
                if idempotency_key:
                    self._db.insert_idempotency(
                        conn, key=idempotency_key, actor=actor.name, endpoint=endpoint,
                        response=json.dumps(result, ensure_ascii=False),
                        created_at=self._now(),
                    )
                return result
        except sqlite3.IntegrityError as exc:
            # 并发下同键请求：首个提交后，其余按重放处理
            if idempotency_key:
                existing = self._db.find_idempotency(idempotency_key)
                if existing:
                    replay = json.loads(existing["response"])
                    replay["meta"] = {**replay.get("meta", {}), "idempotent_replay": True}
                    return replay
            raise ConflictError(f"唯一性约束冲突: {exc}") from exc

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------
    @staticmethod
    def _need(payload: dict, field: str, where: str) -> Any:
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValidationError(f"{where} 缺少必填字段: {field}")
        return value

    @classmethod
    def _validate_unit(cls, raw: dict, where: str) -> dict:
        if not isinstance(raw, dict):
            raise ValidationError(f"{where}: 能力单元必须是对象")
        code = cls._need(raw, "code", where)
        title = cls._need(raw, "title", where)
        level = raw.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or level < 1:
            raise ValidationError(f"{where}.{code}: level 必须是 >= 1 的整数")
        hours = raw.get("credit_hours")
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
            raise ValidationError(f"{where}.{code}: credit_hours 必须是正数")
        scope = raw.get("practical_scope", [])
        if not isinstance(scope, list) or any(
            not isinstance(s, str) or not s.strip() for s in scope
        ):
            raise ValidationError(f"{where}.{code}: practical_scope 必须是非空字符串数组")
        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValidationError(f"{where}.{code}: evidence 必须是数组")
        normalized_evidence = []
        for item in evidence:
            if not isinstance(item, dict):
                raise ValidationError(f"{where}.{code}: 证据要求必须是对象")
            normalized_evidence.append({
                "kind": cls._need(item, "kind", f"{where}.{code}.evidence"),
                "description": cls._need(item, "description", f"{where}.{code}.evidence"),
                "mandatory": bool(item.get("mandatory", True)),
            })
        return {
            "code": code.strip(), "title": title.strip(), "level": level,
            "credit_hours": float(hours),
            "practical_scope": sorted({s.strip() for s in scope}),
            "evidence": normalized_evidence,
        }

    def _create_version_with_units(self, conn, standard_id: str, version_payload: dict,
                                   parent_version_id: str | None) -> dict:
        label = self._need(version_payload, "label", "version")
        units_raw = version_payload.get("units")
        if not isinstance(units_raw, list) or not units_raw:
            raise ValidationError("version.units 必须是非空数组")
        units = [self._validate_unit(u, f"version.{label}") for u in units_raw]
        codes = [u["code"] for u in units]
        if len(codes) != len(set(codes)):
            raise ValidationError(f"版本 {label} 内能力单元代码重复")
        now = self._now()
        version_id = self._new_id("ver")
        self._db.insert_version(
            conn, id=version_id, standard_id=standard_id, version_label=label.strip(),
            status=VersionStatus.PUBLISHED, parent_version_id=parent_version_id,
            published_at=now, created_at=now,
        )
        for unit in units:
            unit_id = self._new_id("unit")
            self._db.insert_unit(
                conn, id=unit_id, version_id=version_id, code=unit["code"],
                title=unit["title"], level=unit["level"],
                credit_hours=unit["credit_hours"],
                practical_scope=unit["practical_scope"], created_at=now,
            )
            for ev in unit["evidence"]:
                self._db.insert_evidence(
                    conn, id=self._new_id("ev"), unit_id=unit_id, kind=ev["kind"],
                    description=ev["description"], mandatory=ev["mandatory"],
                    created_at=now,
                )
        return {"id": version_id, "version_label": label.strip(), "unit_count": len(units)}

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    def import_standard(self, actor: Actor, payload: dict,
                        idempotency_key: str | None = None) -> dict:
        """导入一个标准及其首个发布版本（含能力单元与证据要求）。"""
        self._require(actor, Role.IMPORTER)
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是对象")
        code = self._need(payload, "code", "standard")
        title = self._need(payload, "title", "standard")
        country = self._need(payload, "country", "standard")
        authority = self._need(payload, "authority", "standard")
        version_payload = payload.get("version")
        if not isinstance(version_payload, dict):
            raise ValidationError("standard.version 必须是对象")

        def work(conn):
            if self._db.find_standard_by_code(code):
                raise ConflictError(f"标准代码已存在: {code}")
            now = self._now()
            standard_id = self._new_id("std")
            self._db.insert_standard(
                conn, id=standard_id, code=code.strip(), title=title.strip(),
                country=country.strip(), authority=authority.strip(), created_at=now,
            )
            version = self._create_version_with_units(
                conn, standard_id, version_payload, parent_version_id=None
            )
            self._audit(conn, "standard", standard_id, "imported", actor.name,
                        {"code": code, "version": version["version_label"]})
            return {
                "standard_id": standard_id, "code": code.strip(),
                "version_id": version["id"], "version_label": version["version_label"],
                "unit_count": version["unit_count"],
            }

        return self._execute(actor, "import_standard", idempotency_key, work)

    # ------------------------------------------------------------------
    # 版本发布（源标准更新 → 标记受影响映射）
    # ------------------------------------------------------------------
    def _ancestor_version_ids(self, version_id: str) -> list[str]:
        """沿 parent 链向上收集全部祖先版本（分叉时各分支互不影响）。"""
        chain: list[str] = []
        current = self._db.get_version(version_id)
        while current and current.get("parent_version_id"):
            parent = self._db.get_version(current["parent_version_id"])
            if parent is None:
                break
            chain.append(parent["id"])
            current = parent
        return chain

    def publish_version(self, actor: Actor, standard_code: str, payload: dict,
                        idempotency_key: str | None = None) -> dict:
        """发布标准的新版本；旧版本映射标记受影响，历史决定保持不变。"""
        self._require(actor, Role.IMPORTER)
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是对象")

        def work(conn):
            standard = self._db.find_standard_by_code(standard_code)
            if standard is None:
                raise NotFoundError(f"标准不存在: {standard_code}")
            parent_id = None
            parent_label = payload.get("parent_version_label")
            if parent_label:
                parent = self._db.find_version(standard["id"], parent_label)
                if parent is None:
                    raise NotFoundError(f"父版本不存在: {parent_label}")
                parent_id = parent["id"]
            else:
                published = [v for v in self._db.list_versions(standard["id"])
                             if v["status"] == VersionStatus.PUBLISHED]
                if published:
                    parent_id = sorted(
                        published, key=lambda v: (v["published_at"] or "", v["id"])
                    )[-1]["id"]
            label = self._need(payload, "label", "version")
            if self._db.find_version(standard["id"], label):
                raise ConflictError(f"版本已存在: {label}")
            version = self._create_version_with_units(conn, standard["id"], payload, parent_id)
            if parent_id:
                parent = self._db.get_version(parent_id)
                if parent and parent["status"] == VersionStatus.PUBLISHED:
                    self._db.set_version_status(conn, parent_id, VersionStatus.SUPERSEDED)
            # 源标准更新：祖先链上各版本的映射标记受影响（不触碰历史决定）
            affected = []
            ancestor_ids = self._ancestor_version_ids(version["id"])
            for mapping in self._db.list_mappings_touching_versions(ancestor_ids):
                if mapping["status"] == MappingStatus.REVOKED:
                    continue
                self._db.mark_mapping_affected(conn, mapping["id"], under_review=True)
                conn.execute(
                    "UPDATE mappings SET approval_round = approval_round + 1 WHERE id = ?",
                    (mapping["id"],),
                )
                self._audit(conn, "mapping", mapping["id"], "marked_affected",
                            actor.name, {"by_version": version["id"],
                                         "standard": standard_code})
                affected.append(mapping["id"])
            self._audit(conn, "version", version["id"], "published", actor.name,
                        {"standard": standard_code, "label": version["version_label"],
                         "affected_mappings": affected})
            return {
                "version_id": version["id"], "version_label": version["version_label"],
                "parent_version_id": parent_id, "unit_count": version["unit_count"],
                "affected_mapping_ids": affected,
            }

        return self._execute(actor, "publish_version", idempotency_key, work)

    # ------------------------------------------------------------------
    # 映射
    # ------------------------------------------------------------------
    def create_mapping(self, actor: Actor, payload: dict,
                       idempotency_key: str | None = None) -> dict:
        """建立两个能力单元之间的双向映射（互认按两个方向分别计算）。"""
        self._require(actor, Role.MAPPER)
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是对象")
        source_unit_id = self._need(payload, "source_unit_id", "mapping")
        target_unit_id = self._need(payload, "target_unit_id", "mapping")
        rationale = str(payload.get("rationale", ""))

        def work(conn):
            source = self._db.get_unit(source_unit_id)
            target = self._db.get_unit(target_unit_id)
            if source is None:
                raise NotFoundError(f"能力单元不存在: {source_unit_id}")
            if target is None:
                raise NotFoundError(f"能力单元不存在: {target_unit_id}")
            if source["version_id"] == target["version_id"]:
                raise ValidationError("同一版本内的能力单元不能建立互认映射")
            if self._db.find_mapping(source_unit_id, target_unit_id) or \
                    self._db.find_mapping(target_unit_id, source_unit_id):
                raise ConflictError("两个能力单元之间已存在映射")
            mapping_id = self._new_id("map")
            self._db.insert_mapping(
                conn, id=mapping_id, source_unit_id=source_unit_id,
                target_unit_id=target_unit_id, rationale=rationale,
                status=MappingStatus.PROPOSED, created_by=actor.name,
                created_at=self._now(),
            )
            self._audit(conn, "mapping", mapping_id, "created", actor.name,
                        {"source_unit_id": source_unit_id,
                         "target_unit_id": target_unit_id})
            return {"mapping_id": mapping_id, "status": MappingStatus.PROPOSED}

        return self._execute(actor, "create_mapping", idempotency_key, work)

    # ------------------------------------------------------------------
    # 会签（多方权限 + 幂等）
    # ------------------------------------------------------------------
    def _mapping_parties(self, mapping: dict) -> frozenset[str]:
        source = self._db.get_unit(mapping["source_unit_id"])
        target = self._db.get_unit(mapping["target_unit_id"])
        parties = set()
        for unit in (source, target):
            version = self._db.get_version(unit["version_id"])
            standard = self._db.get_standard(version["standard_id"])
            parties.add(standard["authority"])
        return frozenset(parties)

    def _required_parties(self, subject_type: str, subject: dict) -> frozenset[str]:
        if subject_type == SubjectType.MAPPING:
            return self._mapping_parties(subject)
        version = self._db.get_version(subject["version_id"])
        standard = self._db.get_standard(version["standard_id"])
        return frozenset({standard["authority"], subject["counterparty_authority"]})

    def _get_subject(self, subject_type: str, subject_id: str) -> dict:
        subject = (self._db.get_mapping(subject_id)
                   if subject_type == SubjectType.MAPPING
                   else self._db.get_exception(subject_id))
        if subject is None:
            raise NotFoundError(f"{subject_type} 不存在: {subject_id}")
        return subject

    def _subject_snapshot(self, subject_type: str, subject_id: str,
                          note: str | None = None) -> dict:
        subject = self._get_subject(subject_type, subject_id)
        required = sorted(self._required_parties(subject_type, subject))
        round_no = subject.get("approval_round", 0)
        approvals = [a for a in self._db.list_approvals(subject_type, subject_id)
                     if a.get("round", 0) == round_no]
        snapshot = {
            "subject_type": subject_type, "subject_id": subject_id,
            "status": subject["status"], "required_parties": required,
            "signed_parties": sorted({a["party"] for a in approvals}),
            "approvals": approvals,
        }
        if note:
            snapshot["note"] = note
        return snapshot

    def approve(self, actor: Actor, subject_type: str, subject_id: str, party: str,
                idempotency_key: str | None = None) -> dict:
        """代表机构对映射或例外会签；双方机构各一票后对象生效。"""
        self._require(actor, Role.APPROVER)
        if subject_type not in SubjectType.ALL:
            raise ValidationError(f"不支持的会签对象类型: {subject_type}")
        if not party or not party.strip():
            raise ValidationError("缺少会签机构 party")
        party = party.strip()

        def work(conn):
            subject = self._get_subject(subject_type, subject_id)
            if subject["status"] == MappingStatus.REVOKED or \
                    subject["status"] == ExceptionStatus.REVOKED:
                raise ConflictError("对象已撤销，不能会签")
            required = self._required_parties(subject_type, subject)
            if party not in required:
                raise PermissionDeniedError(
                    f"机构 {party} 不是该对象的会签方（需要: {sorted(required)}）"
                )
            round_no = subject.get("approval_round", 0)
            existing = [a for a in self._db.list_approvals(subject_type, subject_id)
                        if a.get("round", 0) == round_no]
            for ap in existing:
                if ap["actor"] == actor.name and ap["party"] != party:
                    raise PermissionDeniedError("同一操作者不得代表多个机构会签")
            if any(ap["party"] == party for ap in existing):
                return self._subject_snapshot(
                    subject_type, subject_id, note="该机构已会签，请求幂等忽略"
                )
            self._db.insert_approval(
                conn, id=self._new_id("appr"), subject_type=subject_type,
                subject_id=subject_id, party=party, actor=actor.name,
                created_at=self._now(), round=round_no,
            )
            self._audit(conn, subject_type, subject_id, "party_approved",
                        actor.name, {"party": party, "round": round_no})
            signed = {ap["party"] for ap in existing} | {party}
            if set(required) <= signed:
                if subject_type == SubjectType.MAPPING:
                    self._db.update_mapping_status(conn, subject_id, MappingStatus.APPROVED)
                    conn.execute(
                        "UPDATE mappings SET affected_by_update = 0 WHERE id = ?",
                        (subject_id,),
                    )
                else:
                    self._db.update_exception_status(
                        conn, subject_id, ExceptionStatus.APPROVED
                    )
                self._audit(conn, subject_type, subject_id, "approved",
                            actor.name, {"parties": sorted(required)})
            return self._subject_snapshot(subject_type, subject_id)

        return self._execute(actor, f"approve:{subject_type}", idempotency_key, work)

    # ------------------------------------------------------------------
    # 撤销（幂等）
    # ------------------------------------------------------------------
    def revoke(self, actor: Actor, subject_type: str, subject_id: str, reason: str,
               idempotency_key: str | None = None) -> dict:
        """撤销映射或例外；重复撤销返回相同结果。历史决定保持不变。"""
        self._require(actor, Role.ADMIN)
        if subject_type not in SubjectType.ALL:
            raise ValidationError(f"不支持的撤销对象类型: {subject_type}")
        if not reason or not reason.strip():
            raise ValidationError("撤销必须给出原因 reason")

        def work(conn):
            subject = self._get_subject(subject_type, subject_id)
            revoked_status = (MappingStatus.REVOKED if subject_type == SubjectType.MAPPING
                              else ExceptionStatus.REVOKED)
            if subject["status"] == revoked_status:
                return self._subject_snapshot(
                    subject_type, subject_id, note="对象已撤销，请求幂等忽略"
                )
            now = self._now()
            if subject_type == SubjectType.MAPPING:
                self._db.update_mapping_status(
                    conn, subject_id, MappingStatus.REVOKED,
                    revoked_at=now, revoke_reason=reason.strip(),
                )
            else:
                self._db.update_exception_status(
                    conn, subject_id, ExceptionStatus.REVOKED, revoked_at=now,
                )
            self._audit(conn, subject_type, subject_id, "revoked", actor.name,
                        {"reason": reason.strip()})
            return self._subject_snapshot(subject_type, subject_id)

        return self._execute(actor, f"revoke:{subject_type}", idempotency_key, work)

    # ------------------------------------------------------------------
    # 例外
    # ------------------------------------------------------------------
    def propose_exception(self, actor: Actor, payload: dict,
                          idempotency_key: str | None = None) -> dict:
        """专家针对特定版本提出带期限的例外；需双方机构会签后才生效。"""
        self._require(actor, Role.EXPERT)
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是对象")
        code = self._need(payload, "code", "exception")
        version_id = self._need(payload, "version_id", "exception")
        kind = self._need(payload, "kind", "exception")
        reason = self._need(payload, "reason", "exception")
        counterparty = self._need(payload, "counterparty_authority", "exception")
        if kind not in ExceptionKind.ALL:
            raise ValidationError(f"不支持的例外类型: {kind}")
        effective_from = parse_instant(self._need(payload, "effective_from", "exception"))
        effective_until = parse_instant(self._need(payload, "effective_until", "exception"))
        if not effective_from < effective_until:
            raise ValidationError("effective_from 必须早于 effective_until")

        def work(conn):
            version = self._db.get_version(version_id)
            if version is None:
                raise NotFoundError(f"标准版本不存在: {version_id}")
            standard = self._db.get_standard(version["standard_id"])
            if counterparty.strip() == standard["authority"]:
                raise ValidationError("counterparty_authority 必须是互认对方机构")
            mapping_id = payload.get("mapping_id")
            if mapping_id is not None:
                mapping = self._db.get_mapping(mapping_id)
                if mapping is None:
                    raise NotFoundError(f"映射不存在: {mapping_id}")
                touched = {self._db.get_unit(mapping["source_unit_id"])["version_id"],
                           self._db.get_unit(mapping["target_unit_id"])["version_id"]}
                if version_id not in touched:
                    raise ValidationError("例外绑定的映射不涉及该版本")
            if self._db.find_exception_by_code(code):
                raise ConflictError(f"例外代码已存在: {code}")
            exception_id = self._new_id("exc")
            self._db.insert_exception(
                conn, id=exception_id, code=code.strip(), version_id=version_id,
                mapping_id=mapping_id, kind=kind, reason=reason.strip(),
                proposed_by=actor.name, counterparty_authority=counterparty.strip(),
                status=ExceptionStatus.PROPOSED,
                effective_from=format_instant(effective_from),
                effective_until=format_instant(effective_until),
                created_at=self._now(),
            )
            self._audit(conn, "exception", exception_id, "proposed", actor.name,
                        {"code": code, "kind": kind, "version_id": version_id})
            return {"exception_id": exception_id, "code": code.strip(),
                    "status": ExceptionStatus.PROPOSED}

        return self._execute(actor, "propose_exception", idempotency_key, work)

    # ------------------------------------------------------------------
    # 比对
    # ------------------------------------------------------------------
    def _unit_facts(self, unit: dict) -> UnitFacts:
        return UnitFacts(
            id=unit["id"], code=unit["code"], title=unit["title"],
            level=unit["level"], credit_hours=unit["credit_hours"],
            practical_scope=frozenset(unit["practical_scope"]),
        )

    def _active_exceptions(self, mapping: dict, version_ids: list[str]) -> tuple[ActiveException, ...]:
        now = self._clock.now()
        active = []
        for exc in self._db.list_exceptions_for_mapping(mapping["id"], version_ids):
            if exc["status"] != ExceptionStatus.APPROVED:
                continue
            if parse_instant(exc["effective_from"]) <= now < parse_instant(exc["effective_until"]):
                active.append(ActiveException(
                    id=exc["id"], code=exc["code"], kind=exc["kind"], reason=exc["reason"],
                ))
        return tuple(active)

    def compare_versions(self, actor: Actor, version_a_id: str, version_b_id: str,
                         idempotency_key: str | None = None) -> dict:
        """对两个版本间所有已生效映射计算双向互认决定（只追加，不改写历史）。"""
        self._require(actor, *Role.ALL)
        if version_a_id == version_b_id:
            raise ValidationError("比对的两个版本不能相同")
        version_a = self._db.get_version(version_a_id)
        version_b = self._db.get_version(version_b_id)
        if version_a is None:
            raise NotFoundError(f"标准版本不存在: {version_a_id}")
        if version_b is None:
            raise NotFoundError(f"标准版本不存在: {version_b_id}")

        def work(conn):
            mappings = [
                m for m in self._db.list_mappings_between_versions(version_a_id, version_b_id)
                if m["status"] in (MappingStatus.APPROVED, MappingStatus.UNDER_REVIEW)
            ]
            now = self._now()
            comparison_id = self._new_id("cmp")
            results = []
            for mapping in mappings:
                source_unit = self._db.get_unit(mapping["source_unit_id"])
                target_unit = self._db.get_unit(mapping["target_unit_id"])
                if source_unit["version_id"] == version_a_id:
                    unit_a, unit_b = source_unit, target_unit
                else:
                    unit_a, unit_b = target_unit, source_unit
                exceptions = self._active_exceptions(mapping, [version_a_id, version_b_id])
                decisions = []
                for from_unit, to_unit, from_ver, to_ver in (
                    (unit_a, unit_b, version_a_id, version_b_id),
                    (unit_b, unit_a, version_b_id, version_a_id),
                ):
                    result = decide(self._unit_facts(from_unit),
                                    self._unit_facts(to_unit), exceptions)
                    decision_id = self._new_id("dec")
                    self._db.insert_decision(
                        conn, id=decision_id, mapping_id=mapping["id"],
                        from_version_id=from_ver, to_version_id=to_ver,
                        outcome=result.outcome, conditions=result.conditions,
                        details={"dimensions": [vars(d) for d in result.dimensions],
                                 "mapping_status": mapping["status"]},
                        exception_codes=result.applied_exceptions,
                        rule_version=RULE_VERSION, computed_at=now,
                    )
                    decisions.append({
                        "decision_id": decision_id,
                        "from_version_id": from_ver, "to_version_id": to_ver,
                        "outcome": result.outcome,
                        "conditions": list(result.conditions),
                        "applied_exceptions": list(result.applied_exceptions),
                    })
                results.append({"mapping_id": mapping["id"],
                                "mapping_status": mapping["status"],
                                "decisions": decisions})
            self._audit(conn, "comparison", comparison_id, "computed", actor.name, {
                "version_a_id": version_a_id, "version_b_id": version_b_id,
                "mapping_count": len(mappings),
                "decision_ids": [d["decision_id"] for r in results for d in r["decisions"]],
            })
            return {
                "comparison_id": comparison_id,
                "version_a_id": version_a_id, "version_b_id": version_b_id,
                "computed_at": now, "rule_version": RULE_VERSION,
                "results": results,
            }

        return self._execute(actor, "compare_versions", idempotency_key, work)

    # ------------------------------------------------------------------
    # 追溯
    # ------------------------------------------------------------------
    def trace_mapping(self, actor: Actor, mapping_id: str) -> dict:
        """映射的完整追溯：两端单元、历次决定、会签、例外与审计事件。"""
        self._require(actor, *Role.ALL)
        mapping = self._db.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"映射不存在: {mapping_id}")

        def endpoint(unit_id: str) -> dict:
            unit = self._db.get_unit(unit_id)
            version = self._db.get_version(unit["version_id"])
            standard = self._db.get_standard(version["standard_id"])
            return {
                "unit": unit, "evidence": self._db.list_evidence(unit_id),
                "version": version, "standard": standard,
            }

        source = endpoint(mapping["source_unit_id"])
        target = endpoint(mapping["target_unit_id"])
        version_ids = [source["version"]["id"], target["version"]["id"]]
        return {
            "mapping": mapping,
            "source": source,
            "target": target,
            "decisions": self._db.list_decisions(mapping_id),
            "approvals": self._db.list_approvals(SubjectType.MAPPING, mapping_id),
            "exceptions": self._db.list_exceptions_for_mapping(mapping_id, version_ids),
            "events": self._db.list_audit("mapping", mapping_id),
        }

    def equivalence_chain(self, actor: Actor, unit_id: str) -> dict:
        """沿已批准映射遍历等价链；检测循环并显式报告，不会死循环。"""
        self._require(actor, *Role.ALL)
        if self._db.get_unit(unit_id) is None:
            raise NotFoundError(f"能力单元不存在: {unit_id}")
        adjacency: dict[str, list[tuple[str, str]]] = {}

        def neighbors(uid: str) -> list[tuple[str, str]]:
            if uid not in adjacency:
                adjacency[uid] = [
                    (m["target_unit_id"] if m["source_unit_id"] == uid else m["source_unit_id"],
                     m["id"])
                    for m in self._db.list_mappings_for_unit(uid)
                    if m["status"] == MappingStatus.APPROVED
                ]
            return adjacency[uid]

        visited: set[str] = set()
        edges: dict[str, dict] = {}
        cycles: list[list[str]] = []
        seen_cycles: set[frozenset[str]] = set()

        def dfs(uid: str, path: list[str], arrived_via: str | None) -> None:
            visited.add(uid)
            for nxt, mapping_id in neighbors(uid):
                if mapping_id == arrived_via:
                    continue  # 来时的边不是环
                edges.setdefault(mapping_id, {
                    "mapping_id": mapping_id, "from_unit_id": uid, "to_unit_id": nxt,
                })
                if nxt in path:
                    cycle = path[path.index(nxt):] + [nxt]
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
                elif nxt not in visited:
                    dfs(nxt, path + [nxt], mapping_id)

        dfs(unit_id, [unit_id], None)
        return {
            "start_unit_id": unit_id,
            "units": sorted(visited),
            "edges": sorted(edges.values(), key=lambda e: e["mapping_id"]),
            "cycles": cycles,
            "has_cycle": bool(cycles),
        }

    def version_impact(self, actor: Actor, version_id: str) -> dict:
        """某版本相关映射及其受影响标记，用于源标准更新后的复核。"""
        self._require(actor, *Role.ALL)
        if self._db.get_version(version_id) is None:
            raise NotFoundError(f"标准版本不存在: {version_id}")
        mappings = self._db.list_mappings_touching_versions([version_id])
        return {
            "version_id": version_id,
            "mappings": [
                {"mapping_id": m["id"], "status": m["status"],
                 "affected_by_update": m["affected_by_update"]}
                for m in mappings
            ],
        }

    def list_versions(self, actor: Actor, standard_code: str) -> dict:
        """标准的版本谱系（含分叉与状态）。"""
        self._require(actor, *Role.ALL)
        standard = self._db.find_standard_by_code(standard_code)
        if standard is None:
            raise NotFoundError(f"标准不存在: {standard_code}")
        return {"standard": standard, "versions": self._db.list_versions(standard["id"])}

    def list_units(self, actor: Actor, version_id: str) -> dict:
        """版本下的能力单元与证据要求。"""
        self._require(actor, *Role.ALL)
        if self._db.get_version(version_id) is None:
            raise NotFoundError(f"标准版本不存在: {version_id}")
        units = [{**u, "evidence": self._db.list_evidence(u["id"])}
                 for u in self._db.list_units(version_id)]
        return {"version_id": version_id, "units": units}
