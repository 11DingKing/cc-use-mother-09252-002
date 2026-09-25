"""应用服务：导入、比对、会签、撤销与追溯的用例编排。

权限模型（多方约束）：
- admin：引导管理员，隐含全部角色与通配方 "*"；
- authority：标准所属国主管方，负责导入/发布/退役本国标准并参与会签；
- registry：互认登记方，参与会签并可撤销映射与已生效例外；
- expert：专家，可对特定版本的映射提出带期限的例外。

幂等约束：
- 导入按自然键（标准代码、版本标签、单元代码）去重，内容不同则冲突；
- 映射按无序单元对去重，重复创建返回既有映射；
- 会签按 (例外, 幂等键) 与 (例外, 签署方) 双唯一约束保证可安全重放。

历史约束：规则决定只追加不改写；源标准更新仅把受影响映射标记为
affected，例外以独立记录叠加生效，均不回写历史。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from . import domain
from .domain import UnitProfile
from .ports import Clock, IdGenerator, format_instant, parse_instant
from .storage import Database, Repository

ROLES = ("admin", "expert", "authority", "registry")
REGISTRY_PARTY = "registry"
WILDCARD_PARTY = "*"

MAPPING_ACTIVE = "active"
MAPPING_AFFECTED = "affected"
MAPPING_REVOKED = "revoked"


class ServiceError(Exception):
    """应用层错误基类；code 映射到接口层状态码。"""

    code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(ServiceError):
    code = "invalid_request"


class UnauthorizedError(ServiceError):
    code = "unauthorized"


class ForbiddenError(ServiceError):
    code = "forbidden"


class NotFoundError(ServiceError):
    code = "not_found"


class ConflictError(ServiceError):
    code = "conflict"


@dataclass(frozen=True)
class ActorContext:
    """已认证调用者。"""

    id: str
    name: str
    roles: frozenset[str]
    parties: frozenset[str]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MutualRecognitionService:
    """互认服务的应用层入口；所有公开方法自成事务。"""

    def __init__(self, db: Database, clock: Clock, ids: IdGenerator):
        self._db = db
        self._repo = Repository(db)
        self._clock = clock
        self._ids = ids

    # ------------------------------------------------------------------
    # 参与者与认证
    # ------------------------------------------------------------------
    def ensure_bootstrap_admin(self, token: str) -> None:
        """启动时保证存在引导管理员（幂等）。"""
        if self._repo.get_actor_by_name("bootstrap-admin") is not None:
            return
        if not isinstance(token, str) or len(token) < 8:
            raise ValidationError("引导管理员令牌至少 8 个字符")
        with self._db.transaction():
            if self._repo.get_actor_by_name("bootstrap-admin") is not None:
                return
            self._repo.insert_actor(
                id=self._ids.new_id("actor"),
                name="bootstrap-admin",
                token_hash=_hash_token(token),
                roles=list(ROLES),
                parties=[WILDCARD_PARTY],
                created_at=format_instant(self._clock.now()),
            )

    def create_actor(self, caller: ActorContext, *, name: str, token: str,
                     roles: list[str], parties: list[str]) -> dict:
        self._require_role(caller, "admin")
        name = self._require_str(name, "name")
        if not isinstance(token, str) or len(token) < 8:
            raise ValidationError("令牌至少 8 个字符")
        if not isinstance(roles, list) or not roles:
            raise ValidationError("roles 必须为非空数组")
        unknown = set(roles) - set(ROLES)
        if unknown:
            raise ValidationError(f"未知角色：{sorted(unknown)}")
        if not isinstance(parties, list) or any(not isinstance(p, str) or not p for p in parties):
            raise ValidationError("parties 必须为字符串数组")
        now = format_instant(self._clock.now())
        with self._db.transaction():
            if self._repo.get_actor_by_name(name) is not None:
                raise ConflictError(f"参与者名称已存在：{name}")
            actor_id = self._ids.new_id("actor")
            try:
                self._repo.insert_actor(
                    id=actor_id, name=name, token_hash=_hash_token(token),
                    roles=sorted(set(roles)), parties=sorted(set(parties)),
                    created_at=now,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("参与者名称或令牌已被使用") from exc
            self._audit("actor", actor_id, "actor.created", caller.id,
                        {"name": name, "roles": sorted(set(roles))})
            return self._actor_view(self._repo.get_actor(actor_id))

    def authenticate(self, token: str) -> ActorContext:
        if not isinstance(token, str) or not token:
            raise UnauthorizedError("缺少访问令牌")
        row = self._repo.get_actor_by_token_hash(_hash_token(token))
        if row is None:
            raise UnauthorizedError("无效访问令牌")
        return ActorContext(
            id=row["id"], name=row["name"],
            roles=frozenset(row["roles"]), parties=frozenset(row["parties"]),
        )

    # ------------------------------------------------------------------
    # 导入与版本生命周期
    # ------------------------------------------------------------------
    def import_standard(self, caller: ActorContext, payload: dict) -> tuple[dict, dict]:
        """导入标准的一个版本（草稿）。按自然键幂等：内容一致返回既有记录。"""
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须为对象")
        std = payload.get("standard")
        if not isinstance(std, dict):
            raise ValidationError("缺少 standard 对象")
        code = self._require_str(std.get("code"), "standard.code")
        title = self._require_str(std.get("title"), "standard.title")
        country = self._require_str(std.get("country"), "standard.country")
        issuing_body = self._require_str(std.get("issuing_body"), "standard.issuing_body")
        self._require_party(caller, f"authority:{country}")

        ver = payload.get("version")
        if not isinstance(ver, dict):
            raise ValidationError("缺少 version 对象")
        label = self._require_str(ver.get("label"), "version.label")
        parent_id = ver.get("parent_version_id")
        if parent_id is not None:
            parent_id = self._require_str(parent_id, "version.parent_version_id")
        effective_from = None
        if ver.get("effective_from") is not None:
            effective_from = self._parse_time(ver["effective_from"], "version.effective_from")

        units = payload.get("units")
        if not isinstance(units, list) or not units:
            raise ValidationError("units 必须为非空数组")
        normalized = [self._normalize_unit(u, i) for i, u in enumerate(units)]
        content_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        now = format_instant(self._clock.now())

        with self._db.transaction():
            standard = self._repo.get_standard_by_code(code)
            created_standard = False
            if standard is None:
                standard_id = self._ids.new_id("std")
                self._repo.insert_standard(
                    id=standard_id, code=code, title=title, country=country,
                    issuing_body=issuing_body, created_at=now,
                )
                self._audit("standard", standard_id, "standard.imported", caller.id,
                            {"code": code, "country": country})
                created_standard = True
            else:
                standard_id = standard["id"]
                if (standard["title"], standard["country"], standard["issuing_body"]) != (
                        title, country, issuing_body):
                    raise ConflictError(f"标准代码 {code} 已存在且内容不一致")

            version = self._repo.get_version_by_label(standard_id, label)
            created_version = False
            if version is None:
                if parent_id is not None:
                    parent = self._repo.get_version(parent_id)
                    if parent is None:
                        raise ValidationError("父版本不存在")
                    if parent["standard_id"] != standard_id:
                        raise ValidationError("父版本属于其他标准")
                version_id = self._ids.new_id("ver")
                self._repo.insert_version(
                    id=version_id, standard_id=standard_id, version_label=label,
                    parent_version_id=parent_id, status="draft",
                    content_hash=content_hash, effective_from=effective_from,
                    created_at=now,
                )
                for unit in normalized:
                    unit_id = self._ids.new_id("unit")
                    self._repo.insert_unit(
                        id=unit_id, version_id=version_id, code=unit["code"],
                        title=unit["title"], hours=unit["hours"], level=unit["level"],
                        practical_scope=unit["practical_scope"],
                    )
                    for ev in unit["evidence"]:
                        self._repo.insert_evidence(
                            id=self._ids.new_id("ev"), unit_id=unit_id, kind=ev["kind"],
                            detail=ev["detail"], mandatory=ev["mandatory"],
                        )
                self._audit("version", version_id, "version.imported", caller.id,
                            {"standard_id": standard_id, "label": label,
                             "parent_version_id": parent_id})
                created_version = True
            else:
                version_id = version["id"]
                if version["content_hash"] != content_hash or \
                        version["parent_version_id"] != parent_id:
                    raise ConflictError(f"版本 {label} 已存在且内容不一致")

            view = {
                "standard": self._standard_view(standard_id),
                "version": self._version_view(version_id),
                "units": [self._unit_view(u["id"]) for u in self._repo.list_units(version_id)],
            }
            return view, {"standard": created_standard, "version": created_version}

    def publish_version(self, caller: ActorContext, version_id: str) -> dict:
        """发布版本；同源标准其他版本涉及的活跃映射标记为受影响（不改写历史）。"""
        with self._db.transaction():
            version = self._get_version_or_404(version_id)
            standard = self._repo.get_standard(version["standard_id"])
            self._require_party(caller, f"authority:{standard['country']}")
            if version["status"] != "draft":
                raise ConflictError(f"仅草稿版本可发布（当前：{version['status']}）")
            now = format_instant(self._clock.now())
            self._repo.update_version_status(version_id, "published", published_at=now)
            self._audit("version", version_id, "version.published", caller.id,
                        {"standard_id": standard["id"], "label": version["version_label"]})
            affected = self._mark_affected(
                standard["id"], exclude_version_id=version_id,
                cause={"cause_version_id": version_id, "cause_standard_id": standard["id"]},
                actor_id=caller.id,
            )
            return {"version": self._version_view(version_id),
                    "affected_mapping_ids": affected}

    def retire_version(self, caller: ActorContext, version_id: str) -> dict:
        """退役版本；引用该版本单元的活跃映射标记为受影响。"""
        with self._db.transaction():
            version = self._get_version_or_404(version_id)
            standard = self._repo.get_standard(version["standard_id"])
            self._require_party(caller, f"authority:{standard['country']}")
            if version["status"] != "published":
                raise ConflictError(f"仅已发布版本可退役（当前：{version['status']}）")
            now = format_instant(self._clock.now())
            self._repo.update_version_status(version_id, "retired", retired_at=now)
            self._audit("version", version_id, "version.retired", caller.id,
                        {"standard_id": standard["id"], "label": version["version_label"]})
            affected = []
            for m in self._repo.list_mappings_touching_versions([version_id], MAPPING_ACTIVE):
                self._repo.update_mapping_status(m["id"], MAPPING_AFFECTED)
                self._audit("mapping", m["id"], "mapping.affected", caller.id,
                            {"cause_version_id": version_id, "cause": "version_retired"})
                affected.append(m["id"])
            return {"version": self._version_view(version_id),
                    "affected_mapping_ids": affected}

    def get_standard_view(self, caller: ActorContext, standard_id: str) -> dict:
        standard = self._repo.get_standard(standard_id)
        if standard is None:
            raise NotFoundError(f"标准不存在：{standard_id}")
        return {
            "standard": self._standard_view(standard_id),
            "versions": [self._version_view(v["id"])
                         for v in self._repo.list_versions(standard_id)],
        }

    def get_version_view(self, caller: ActorContext, version_id: str) -> dict:
        self._get_version_or_404(version_id)
        return {
            "version": self._version_view(version_id),
            "units": [self._unit_view(u["id"]) for u in self._repo.list_units(version_id)],
        }

    def list_affected_mappings(self, caller: ActorContext, version_id: str) -> dict:
        self._get_version_or_404(version_id)
        at = self._clock.now()
        rows = self._repo.list_mappings_touching_versions([version_id], MAPPING_AFFECTED)
        return {"version_id": version_id,
                "affected": [self._mapping_view(m, at) for m in rows]}

    # ------------------------------------------------------------------
    # 映射与比对
    # ------------------------------------------------------------------
    def create_mapping(self, caller: ActorContext, unit_x_id: str, unit_y_id: str
                       ) -> tuple[dict, bool]:
        """建立双向映射并按规则计算两个方向；重复创建返回既有映射（幂等）。"""
        if unit_x_id == unit_y_id:
            raise ValidationError("不能映射同一能力单元")
        ux = self._unit_context_or_404(unit_x_id)
        uy = self._unit_context_or_404(unit_y_id)
        for u in (ux, uy):
            if u["version_status"] != "published":
                raise ValidationError("仅已发布版本的能力单元可建立映射")
        self._require_any_party(caller, {
            f"authority:{ux['standard_country']}",
            f"authority:{uy['standard_country']}",
            REGISTRY_PARTY,
        })
        unit_a, unit_b = sorted([unit_x_id, unit_y_id])
        with self._db.transaction():
            existing = self._repo.find_mapping(unit_a, unit_b)
            if existing is not None:
                return self._mapping_view(existing, self._clock.now()), False
            mapping_id = self._ids.new_id("map")
            now = format_instant(self._clock.now())
            try:
                self._repo.insert_mapping(
                    id=mapping_id, unit_a_id=unit_a, unit_b_id=unit_b,
                    status=MAPPING_ACTIVE, created_by=caller.id, created_at=now,
                )
            except sqlite3.IntegrityError:
                # 并发下另一事务已创建同一映射
                pass
            existing = self._repo.find_mapping(unit_a, unit_b)
            if existing["id"] != mapping_id:
                return self._mapping_view(existing, self._clock.now()), False
            self._record_decisions(mapping_id, seq=1, decided_at=now)
            self._audit("mapping", mapping_id, "mapping.created", caller.id,
                        {"unit_a_id": unit_a, "unit_b_id": unit_b})
            return self._mapping_view(existing, self._clock.now()), True

    def compare_units(self, caller: ActorContext, source_unit_id: str,
                      target_unit_id: str) -> dict:
        """即席比对两个能力单元（不落库）。"""
        source = self._unit_profile(self._unit_context_or_404(source_unit_id))
        target = self._unit_profile(self._unit_context_or_404(target_unit_id))
        forward = domain.evaluate_direction(source, target)
        backward = domain.evaluate_direction(target, source)
        return {
            "source_unit_id": source_unit_id,
            "target_unit_id": target_unit_id,
            "rules_version": domain.RULES_VERSION,
            "source_to_target": self._evaluation_view(forward),
            "target_to_source": self._evaluation_view(backward),
            "mutual_outcome": domain.worse(forward.outcome, backward.outcome),
        }

    def recompute_mapping(self, caller: ActorContext, mapping_id: str) -> dict:
        """重新计算映射：追加新序号决定（历史保留），状态回到 active。"""
        with self._db.transaction():
            mapping = self._get_mapping_or_404(mapping_id)
            if mapping["status"] == MAPPING_REVOKED:
                raise ConflictError("映射已撤销，无法重新计算")
            ux = self._unit_context_or_404(mapping["unit_a_id"])
            uy = self._unit_context_or_404(mapping["unit_b_id"])
            self._require_any_party(caller, {
                f"authority:{ux['standard_country']}",
                f"authority:{uy['standard_country']}",
                REGISTRY_PARTY,
            })
            seq = self._repo.max_decision_seq(mapping_id) + 1
            now = format_instant(self._clock.now())
            self._record_decisions(mapping_id, seq=seq, decided_at=now)
            if mapping["status"] != MAPPING_ACTIVE:
                self._repo.update_mapping_status(mapping_id, MAPPING_ACTIVE)
            self._audit("mapping", mapping_id, "mapping.recomputed", caller.id, {"seq": seq})
            return self._mapping_view(self._repo.get_mapping(mapping_id), self._clock.now())

    def revoke_mapping(self, caller: ActorContext, mapping_id: str, reason: str = "") -> dict:
        """撤销映射（登记方/管理员）；重复撤销幂等返回当前状态。"""
        with self._db.transaction():
            mapping = self._get_mapping_or_404(mapping_id)
            self._require_registry(caller)
            if mapping["status"] != MAPPING_REVOKED:
                self._repo.update_mapping_status(mapping_id, MAPPING_REVOKED)
                self._audit("mapping", mapping_id, "mapping.revoked", caller.id,
                            {"reason": reason})
            return self._mapping_view(self._repo.get_mapping(mapping_id), self._clock.now())

    def get_mapping_view(self, caller: ActorContext, mapping_id: str) -> dict:
        mapping = self._get_mapping_or_404(mapping_id)
        return self._mapping_view(mapping, self._clock.now())

    def recognition_path(self, caller: ActorContext, from_unit_id: str,
                         to_unit_id: str) -> dict:
        """在活跃映射图上寻找互认链；访问集保证循环映射下必然终止。"""
        self._unit_context_or_404(from_unit_id)
        self._unit_context_or_404(to_unit_id)
        if from_unit_id == to_unit_id:
            return {"found": True, "path": [from_unit_id], "hops": []}
        at = self._clock.now()
        visited = {from_unit_id}
        queue: deque = deque([(from_unit_id, [from_unit_id], [])])
        while queue:
            node, path, hops = queue.popleft()
            for m in self._repo.list_mappings_of_unit(node, MAPPING_ACTIVE):
                if m["unit_a_id"] == node:
                    other, direction = m["unit_b_id"], domain.DIRECTION_A_TO_B
                else:
                    other, direction = m["unit_a_id"], domain.DIRECTION_B_TO_A
                if other in visited:
                    continue  # 循环映射：跳过已访问节点
                effective = self._effective_direction(m, direction, at)
                if effective["outcome"] == domain.OUTCOME_NONE:
                    continue  # 不可互认的方向不构成边
                new_path = path + [other]
                new_hops = hops + [{
                    "mapping_id": m["id"], "from_unit_id": node, "to_unit_id": other,
                    "outcome": effective["outcome"], "source": effective["source"],
                }]
                if other == to_unit_id:
                    return {"found": True, "path": new_path, "hops": new_hops}
                visited.add(other)
                queue.append((other, new_path, new_hops))
        return {"found": False, "path": [], "hops": []}

    def trace_mapping(self, caller: ActorContext, mapping_id: str) -> dict:
        """追溯：映射当前视图 + 全部历史决定 + 例外与会签 + 审计事件。"""
        mapping = self._get_mapping_or_404(mapping_id)
        at = self._clock.now()
        exceptions = [self._exception_view(e, at) for e in self._repo.list_exceptions(mapping_id)]
        events = self._repo.list_events("mapping", mapping_id)
        for exc in exceptions:
            events.extend(self._repo.list_events("exception", exc["id"]))
        events.sort(key=lambda e: (e["created_at"], e["id"]))
        return {
            "mapping": self._mapping_view(mapping, at),
            "decisions": self._repo.list_decisions(mapping_id),
            "exceptions": exceptions,
            "events": events,
        }

    # ------------------------------------------------------------------
    # 例外与会签
    # ------------------------------------------------------------------
    def propose_exception(self, caller: ActorContext, payload: dict) -> dict:
        """专家对特定版本映射提出带期限的例外；会签方在此刻快照。"""
        self._require_role(caller, "expert")
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须为对象")
        mapping_id = self._require_str(payload.get("mapping_id"), "mapping_id")
        mapping = self._get_mapping_or_404(mapping_id)
        if mapping["status"] == MAPPING_REVOKED:
            raise ConflictError("映射已撤销，不能提出例外")
        direction = self._require_str(payload.get("direction"), "direction")
        if direction not in (domain.DIRECTION_A_TO_B, domain.DIRECTION_B_TO_A,
                             domain.DIRECTION_BOTH):
            raise ValidationError("direction 必须为 A_TO_B / B_TO_A / BOTH")
        effect = self._require_str(payload.get("effect"), "effect")
        if effect not in domain.OUTCOMES:
            raise ValidationError(f"effect 必须为 {list(domain.OUTCOMES)} 之一")
        conditions = payload.get("conditions") or []
        if not isinstance(conditions, list) or \
                any(not isinstance(c, str) or not c for c in conditions):
            raise ValidationError("conditions 必须为非空字符串数组")
        if effect == domain.OUTCOME_CONDITIONAL and not conditions:
            raise ValidationError("附条件互认必须给出条件清单")
        reason = self._require_str(payload.get("reason"), "reason")
        valid_from = self._parse_time(payload.get("valid_from"), "valid_from")
        valid_until = self._parse_time(payload.get("valid_until"), "valid_until")
        if not valid_from < valid_until:
            raise ValidationError("生效起点必须早于生效终点")

        ux = self._unit_context_or_404(mapping["unit_a_id"])
        uy = self._unit_context_or_404(mapping["unit_b_id"])
        required = sorted({
            f"authority:{ux['standard_country']}",
            f"authority:{uy['standard_country']}",
            REGISTRY_PARTY,
        })
        now = format_instant(self._clock.now())
        with self._db.transaction():
            exception_id = self._ids.new_id("exc")
            self._repo.insert_exception(
                id=exception_id, mapping_id=mapping_id, direction=direction,
                effect=effect, conditions=conditions, reason=reason,
                required_parties=required, proposed_by=caller.id, status="proposed",
                valid_from=valid_from, valid_until=valid_until, created_at=now,
            )
            self._audit("exception", exception_id, "exception.proposed", caller.id,
                        {"mapping_id": mapping_id, "effect": effect,
                         "valid_from": valid_from, "valid_until": valid_until,
                         "required_parties": required})
            return self._exception_view(self._repo.get_exception(exception_id),
                                        self._clock.now())

    def approve_exception(self, caller: ActorContext, exception_id: str, *,
                          party: str, decision: str, idempotency_key: str
                          ) -> tuple[dict, bool]:
        """会签：多方权限 + 幂等。返回 (会签记录, 是否新建)。"""
        party = self._require_str(party, "party")
        if decision not in ("approve", "reject"):
            raise ValidationError("decision 必须为 approve / reject")
        idempotency_key = self._require_str(idempotency_key, "idempotency_key")
        self._require_party(caller, party)
        with self._db.transaction():
            exc = self._repo.get_exception(exception_id)
            if exc is None:
                raise NotFoundError(f"例外不存在：{exception_id}")
            if party not in exc["required_parties"]:
                raise ValidationError(f"签署方 {party} 不在该例外的会签要求中")

            existing = self._repo.get_approval_by_key(exception_id, idempotency_key)
            if existing is not None:
                # 幂等重放：键相同则内容必须一致，否则视为冲突
                if existing["party"] != party or existing["decision"] != decision:
                    raise ConflictError("幂等键已被不同请求使用")
                return self._approval_view(existing), False
            if exc["status"] != "proposed":
                raise ConflictError(f"该例外已不在会签阶段（当前：{exc['status']}）")
            if self._repo.get_approval_by_party(exception_id, party) is not None:
                raise ConflictError(f"签署方 {party} 已会签")

            now = format_instant(self._clock.now())
            approval_id = self._ids.new_id("apr")
            try:
                self._repo.insert_approval(
                    id=approval_id, exception_id=exception_id, party=party,
                    actor_id=caller.id, decision=decision,
                    idempotency_key=idempotency_key, created_at=now,
                )
            except sqlite3.IntegrityError as exc_err:
                raise ConflictError("会签冲突：该方已会签或幂等键已被使用") from exc_err
            self._audit("exception", exception_id, "exception.cosigned", caller.id,
                        {"party": party, "decision": decision})

            if decision == "reject":
                self._repo.update_exception_status(exception_id, "rejected", decided_at=now)
                self._audit("exception", exception_id, "exception.rejected", caller.id,
                            {"party": party})
            else:
                approved = {a["party"] for a in self._repo.list_approvals(exception_id)
                            if a["decision"] == "approve"}
                if set(exc["required_parties"]) <= approved:
                    self._repo.update_exception_status(exception_id, "approved",
                                                       decided_at=now)
                    self._audit("exception", exception_id, "exception.activated",
                                caller.id, {"approved_parties": sorted(approved)})
            return self._approval_view(self._repo.get_approval_by_key(
                exception_id, idempotency_key)), True

    def revoke_exception(self, caller: ActorContext, exception_id: str,
                         reason: str = "") -> dict:
        """撤销例外：提案方可撤回未决例外，已批准例外仅登记方/管理员可撤销。"""
        with self._db.transaction():
            exc = self._repo.get_exception(exception_id)
            if exc is None:
                raise NotFoundError(f"例外不存在：{exception_id}")
            if exc["status"] == "revoked":
                return self._exception_view(exc, self._clock.now())  # 幂等
            if exc["status"] == "rejected":
                raise ConflictError("已被拒绝的例外无需撤销")
            if exc["status"] == "proposed":
                if caller.id != exc["proposed_by"] and not self._is_registry(caller):
                    raise ForbiddenError("仅提案方或登记方可撤回未决例外")
            else:  # approved
                if not self._is_registry(caller):
                    raise ForbiddenError("仅登记方或管理员可撤销已批准例外")
            self._repo.update_exception_status(exception_id, "revoked")
            self._audit("exception", exception_id, "exception.revoked", caller.id,
                        {"reason": reason})
            return self._exception_view(self._repo.get_exception(exception_id),
                                        self._clock.now())

    def get_exception(self, caller: ActorContext, exception_id: str) -> dict:
        exc = self._repo.get_exception(exception_id)
        if exc is None:
            raise NotFoundError(f"例外不存在：{exception_id}")
        return self._exception_view(exc, self._clock.now())

    # ------------------------------------------------------------------
    # 内部：权限
    # ------------------------------------------------------------------
    @staticmethod
    def _require_role(actor: ActorContext, role: str) -> None:
        if role in actor.roles or "admin" in actor.roles:
            return
        raise ForbiddenError(f"需要角色：{role}")

    @staticmethod
    def _require_party(actor: ActorContext, party: str) -> None:
        if "admin" in actor.roles or WILDCARD_PARTY in actor.parties or party in actor.parties:
            return
        raise ForbiddenError(f"需要签署方权限：{party}")

    @staticmethod
    def _require_any_party(actor: ActorContext, parties: set[str]) -> None:
        if "admin" in actor.roles or WILDCARD_PARTY in actor.parties:
            return
        if actor.parties & parties:
            return
        raise ForbiddenError(f"需要以下任一签署方权限：{sorted(parties)}")

    def _require_registry(self, actor: ActorContext) -> None:
        if not self._is_registry(actor):
            raise ForbiddenError("需要登记方或管理员权限")

    @staticmethod
    def _is_registry(actor: ActorContext) -> bool:
        return ("admin" in actor.roles or WILDCARD_PARTY in actor.parties
                or REGISTRY_PARTY in actor.parties)

    # ------------------------------------------------------------------
    # 内部：校验与读取
    # ------------------------------------------------------------------
    @staticmethod
    def _require_str(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"字段 {field} 必须为非空字符串")
        return value.strip()

    @staticmethod
    def _parse_time(value: Any, field: str) -> str:
        try:
            return format_instant(parse_instant(value))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"字段 {field} 无效：{exc}") from exc

    @staticmethod
    def _normalize_unit(unit: Any, index: int) -> dict:
        if not isinstance(unit, dict):
            raise ValidationError(f"units[{index}] 必须为对象")
        code = MutualRecognitionService._require_str(unit.get("code"), f"units[{index}].code")
        title = MutualRecognitionService._require_str(unit.get("title"), f"units[{index}].title")
        hours = unit.get("hours")
        if not isinstance(hours, int) or isinstance(hours, bool) or hours < 0:
            raise ValidationError(f"units[{index}].hours 必须为非负整数")
        level = unit.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 5:
            raise ValidationError(f"units[{index}].level 必须为 1..5 的整数")
        scope = unit.get("practical_scope") or []
        if not isinstance(scope, list) or any(not isinstance(s, str) or not s for s in scope):
            raise ValidationError(f"units[{index}].practical_scope 必须为字符串数组")
        evidence = unit.get("evidence") or []
        if not isinstance(evidence, list):
            raise ValidationError(f"units[{index}].evidence 必须为数组")
        normalized_ev = []
        seen_kinds: set[str] = set()
        for j, ev in enumerate(evidence):
            if not isinstance(ev, dict):
                raise ValidationError(f"units[{index}].evidence[{j}] 必须为对象")
            kind = MutualRecognitionService._require_str(
                ev.get("kind"), f"units[{index}].evidence[{j}].kind")
            if kind in seen_kinds:
                raise ValidationError(f"units[{index}] 证据类型重复：{kind}")
            seen_kinds.add(kind)
            detail = ev.get("detail", "")
            if not isinstance(detail, str):
                raise ValidationError(f"units[{index}].evidence[{j}].detail 必须为字符串")
            mandatory = ev.get("mandatory", True)
            if not isinstance(mandatory, bool):
                raise ValidationError(f"units[{index}].evidence[{j}].mandatory 必须为布尔值")
            normalized_ev.append({"kind": kind, "detail": detail, "mandatory": mandatory})
        return {
            "code": code, "title": title, "hours": hours, "level": level,
            "practical_scope": sorted(set(scope)), "evidence": normalized_ev,
        }

    def _get_version_or_404(self, version_id: str) -> dict:
        version = self._repo.get_version(version_id)
        if version is None:
            raise NotFoundError(f"版本不存在：{version_id}")
        return version

    def _get_mapping_or_404(self, mapping_id: str) -> dict:
        mapping = self._repo.get_mapping(mapping_id)
        if mapping is None:
            raise NotFoundError(f"映射不存在：{mapping_id}")
        return mapping

    def _unit_context_or_404(self, unit_id: str) -> dict:
        ctx = self._repo.get_unit_context(unit_id)
        if ctx is None:
            raise NotFoundError(f"能力单元不存在：{unit_id}")
        return ctx

    def _unit_profile(self, ctx: dict) -> UnitProfile:
        evidence = self._repo.list_evidence(ctx["id"])
        return UnitProfile(
            unit_id=ctx["id"], code=ctx["code"], title=ctx["title"],
            hours=ctx["hours"], level=ctx["level"],
            practical_scope=frozenset(ctx["practical_scope"]),
            mandatory_evidence=frozenset(e["kind"] for e in evidence if e["mandatory"]),
        )

    def _record_decisions(self, mapping_id: str, seq: int, decided_at: str) -> None:
        mapping = self._repo.get_mapping(mapping_id)
        profiles = {
            domain.DIRECTION_A_TO_B: (
                self._unit_profile(self._unit_context_or_404(mapping["unit_a_id"])),
                self._unit_profile(self._unit_context_or_404(mapping["unit_b_id"])),
            ),
            domain.DIRECTION_B_TO_A: (
                self._unit_profile(self._unit_context_or_404(mapping["unit_b_id"])),
                self._unit_profile(self._unit_context_or_404(mapping["unit_a_id"])),
            ),
        }
        for direction, (source, target) in profiles.items():
            ev = domain.evaluate_direction(source, target)
            self._repo.insert_decision(
                id=self._ids.new_id("dec"), mapping_id=mapping_id, seq=seq,
                direction=direction, outcome=ev.outcome, conditions=list(ev.conditions),
                source=f"rules:{domain.RULES_VERSION}", rationale=ev.rationale,
                decided_at=decided_at,
            )

    def _mark_affected(self, standard_id: str, *, exclude_version_id: str,
                       cause: dict, actor_id: str) -> list[str]:
        sibling_ids = [v["id"] for v in self._repo.list_versions(standard_id)
                       if v["id"] != exclude_version_id]
        affected = []
        for m in self._repo.list_mappings_touching_versions(sibling_ids, MAPPING_ACTIVE):
            self._repo.update_mapping_status(m["id"], MAPPING_AFFECTED)
            self._audit("mapping", m["id"], "mapping.affected", actor_id, dict(cause))
            affected.append(m["id"])
        return affected

    def _effective_direction(self, mapping: dict, direction: str, at) -> dict:
        """某方向在指定瞬时的有效结论：生效中的例外优先，否则取最新规则决定。"""
        exc = self._repo.find_active_exception(mapping["id"], direction, format_instant(at))
        if exc is not None:
            return {
                "outcome": exc["effect"], "conditions": exc["conditions"],
                "source": f"exception:{exc['id']}", "rationale": exc["reason"],
            }
        dec = self._repo.latest_decision(mapping["id"], direction)
        if dec is None:
            return {"outcome": domain.OUTCOME_NONE, "conditions": [],
                    "source": "none", "rationale": "尚无评估决定"}
        return {"outcome": dec["outcome"], "conditions": dec["conditions"],
                "source": dec["source"], "rationale": dec["rationale"]}

    def _audit(self, entity_type: str, entity_id: str, action: str,
               actor_id: Optional[str], payload: dict) -> None:
        self._repo.insert_event(
            id=self._ids.new_id("aud"), entity_type=entity_type, entity_id=entity_id,
            action=action, actor_id=actor_id, payload=payload,
            created_at=format_instant(self._clock.now()),
        )

    # ------------------------------------------------------------------
    # 内部：视图组装
    # ------------------------------------------------------------------
    @staticmethod
    def _evaluation_view(ev: domain.Evaluation) -> dict:
        return {"outcome": ev.outcome, "conditions": list(ev.conditions),
                "rationale": ev.rationale}

    @staticmethod
    def _actor_view(row: dict) -> dict:
        return {"id": row["id"], "name": row["name"], "roles": row["roles"],
                "parties": row["parties"], "created_at": row["created_at"]}

    def _standard_view(self, standard_id: str) -> dict:
        s = self._repo.get_standard(standard_id)
        return {"id": s["id"], "code": s["code"], "title": s["title"],
                "country": s["country"], "issuing_body": s["issuing_body"],
                "created_at": s["created_at"]}

    def _version_view(self, version_id: str) -> dict:
        v = self._repo.get_version(version_id)
        return {"id": v["id"], "standard_id": v["standard_id"],
                "label": v["version_label"], "status": v["status"],
                "parent_version_id": v["parent_version_id"],
                "effective_from": v["effective_from"], "created_at": v["created_at"],
                "published_at": v["published_at"], "retired_at": v["retired_at"]}

    def _unit_view(self, unit_id: str) -> dict:
        ctx = self._repo.get_unit_context(unit_id)
        return {
            "id": ctx["id"], "code": ctx["code"], "title": ctx["title"],
            "hours": ctx["hours"], "level": ctx["level"],
            "practical_scope": ctx["practical_scope"],
            "evidence": [
                {"kind": e["kind"], "detail": e["detail"], "mandatory": e["mandatory"]}
                for e in self._repo.list_evidence(unit_id)
            ],
            "version": {"id": ctx["version_id"], "label": ctx["version_label"],
                        "status": ctx["version_status"]},
            "standard": {"id": ctx["standard_id"], "code": ctx["standard_code"],
                         "title": ctx["standard_title"], "country": ctx["standard_country"]},
        }

    def _mapping_view(self, mapping: dict, at) -> dict:
        directions = {
            d: self._effective_direction(mapping, d, at) for d in domain.DIRECTIONS
        }
        mutual = domain.worse(directions[domain.DIRECTION_A_TO_B]["outcome"],
                              directions[domain.DIRECTION_B_TO_A]["outcome"])
        return {
            "id": mapping["id"], "status": mapping["status"],
            "created_by": mapping["created_by"], "created_at": mapping["created_at"],
            "unit_a": self._unit_view(mapping["unit_a_id"]),
            "unit_b": self._unit_view(mapping["unit_b_id"]),
            "directions": directions,
            "mutual_outcome": mutual,
            "evaluated_at": format_instant(at),
        }

    def _exception_view(self, exc: dict, at) -> dict:
        approvals = self._repo.list_approvals(exc["id"])
        approved = {a["party"] for a in approvals if a["decision"] == "approve"}
        remaining = [p for p in exc["required_parties"] if p not in approved]
        now = format_instant(at)
        status = exc["status"]
        if status == "approved":
            if now < exc["valid_from"]:
                effective_status = "approved_pending"
            elif now >= exc["valid_until"]:
                effective_status = "expired"
            else:
                effective_status = "effective"
        else:
            effective_status = status
        return {
            "id": exc["id"], "mapping_id": exc["mapping_id"],
            "direction": exc["direction"], "effect": exc["effect"],
            "conditions": exc["conditions"], "reason": exc["reason"],
            "required_parties": exc["required_parties"],
            "remaining_parties": remaining,
            "proposed_by": exc["proposed_by"], "status": status,
            "effective_status": effective_status,
            "valid_from": exc["valid_from"], "valid_until": exc["valid_until"],
            "created_at": exc["created_at"], "decided_at": exc["decided_at"],
            "approvals": [self._approval_view(a) for a in approvals],
        }

    @staticmethod
    def _approval_view(row: dict) -> dict:
        return {"id": row["id"], "exception_id": row["exception_id"],
                "party": row["party"], "actor_id": row["actor_id"],
                "decision": row["decision"], "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"]}
