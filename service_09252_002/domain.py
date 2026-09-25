"""领域模型：标准、版本、能力单元、映射、决定与例外的核心概念。"""
from __future__ import annotations

from dataclasses import dataclass, field


class VersionStatus:
    """标准版本的生命周期。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    ALL = frozenset({DRAFT, PUBLISHED, SUPERSEDED})


class MappingStatus:
    """映射的生命周期：提议 → 会签通过 → （源标准更新后）待复核 / 撤销。"""

    PROPOSED = "proposed"
    APPROVED = "approved"
    UNDER_REVIEW = "under_review"
    REVOKED = "revoked"
    ALL = frozenset({PROPOSED, APPROVED, UNDER_REVIEW, REVOKED})


class Outcome:
    """互认结论。"""

    FULL = "full"  # 完全互认
    CONDITIONAL = "conditional"  # 附条件互认
    NONE = "none"  # 不可互认
    ALL = frozenset({FULL, CONDITIONAL, NONE})


class ExceptionKind:
    """例外类型：对规则引擎的受控宽限。"""

    FORCE_FULL = "force_full"  # 在期限内直接按完全互认处理
    WAIVE_HOURS = "waive_hours"  # 豁免学时差距
    WAIVE_SCOPE = "waive_scope"  # 豁免实操范围缺项
    WAIVE_LEVEL = "waive_level"  # 豁免等级差距
    ALL = frozenset({FORCE_FULL, WAIVE_HOURS, WAIVE_SCOPE, WAIVE_LEVEL})


class ExceptionStatus:
    PROPOSED = "proposed"
    APPROVED = "approved"
    REVOKED = "revoked"
    ALL = frozenset({PROPOSED, APPROVED, REVOKED})


class SubjectType:
    """可会签 / 可撤销的对象类型。"""

    MAPPING = "mapping"
    EXCEPTION = "exception"
    ALL = frozenset({MAPPING, EXCEPTION})


class Role:
    """操作者角色；会签还要求操作者归属对应机构。"""

    IMPORTER = "importer"  # 导入标准、发布新版本
    MAPPER = "mapper"  # 建立映射
    EXPERT = "expert"  # 提出例外
    APPROVER = "approver"  # 代表机构会签
    ADMIN = "admin"  # 撤销
    ALL = frozenset({IMPORTER, MAPPER, EXPERT, APPROVER, ADMIN})


@dataclass(frozen=True)
class Actor:
    """一次请求的操作者。角色集合由接口边界从认证信息解析。"""

    name: str
    roles: frozenset[str]

    def has_any(self, *roles: str) -> bool:
        return bool(set(self.roles) & set(roles))


@dataclass(frozen=True)
class UnitFacts:
    """规则引擎所需的能力单元事实，与持久化解耦。"""

    id: str
    code: str
    title: str
    level: int
    credit_hours: float
    practical_scope: frozenset[str]


@dataclass(frozen=True)
class ActiveException:
    """比对时处于生效窗口内的例外。"""

    id: str
    code: str
    kind: str
    reason: str
