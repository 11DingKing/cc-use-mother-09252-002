"""互认规则引擎：纯函数，不触碰持久化与时钟。

输入两个能力单元的事实与生效中的例外，输出互认结论：
- 完全互认（full）：学时、实操范围、等级三个维度全部满足；
- 附条件互认（conditional）：存在可弥补的差距，结论附带补足条件；
- 不可互认（none）：任一维度差距不可弥补。
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import ActiveException, ExceptionKind, Outcome, UnitFacts

RULE_VERSION = "1.0.0"

# 学时比阈值：源学时 / 目标学时
HOURS_FULL_RATIO = 0.9
HOURS_CONDITIONAL_RATIO = 0.6
# 等级差：相差 1 级可附条件，相差 2 级及以上不可互认
MAX_CONDITIONAL_LEVEL_GAP = 1


@dataclass(frozen=True)
class Dimension:
    """单个比较维度的判定。"""

    name: str
    verdict: str  # Outcome 之一
    note: str


@dataclass(frozen=True)
class DecisionResult:
    """一次比对的结果。conditions 在 conditional 时为补足条件，
    在 none 时为否决原因；applied_exceptions 记录参与判定的例外。"""

    outcome: str
    conditions: tuple[str, ...]
    dimensions: tuple[Dimension, ...]
    applied_exceptions: tuple[str, ...]  # 例外 code
    rule_version: str = RULE_VERSION


def _hours_dimension(source: UnitFacts, target: UnitFacts, waived: bool) -> Dimension:
    if waived:
        return Dimension("hours", Outcome.FULL, "学时差距已被生效例外豁免")
    ratio = source.credit_hours / target.credit_hours
    if ratio >= HOURS_FULL_RATIO:
        return Dimension(
            "hours", Outcome.FULL,
            f"学时 {source.credit_hours:g}h 覆盖目标 {target.credit_hours:g}h（比例 {ratio:.2f}）",
        )
    if ratio >= HOURS_CONDITIONAL_RATIO:
        gap = target.credit_hours - source.credit_hours
        return Dimension(
            "hours", Outcome.CONDITIONAL,
            f"需补足学时 {gap:g}h（源 {source.credit_hours:g}h / 目标 {target.credit_hours:g}h）",
        )
    return Dimension(
        "hours", Outcome.NONE,
        f"学时差距不可弥补（源 {source.credit_hours:g}h / 目标 {target.credit_hours:g}h，"
        f"比例 {ratio:.2f} 低于 {HOURS_CONDITIONAL_RATIO}）",
    )


def _scope_dimension(source: UnitFacts, target: UnitFacts, waived: bool) -> Dimension:
    if waived:
        return Dimension("scope", Outcome.FULL, "实操范围缺项已被生效例外豁免")
    missing = sorted(target.practical_scope - source.practical_scope)
    if not missing:
        return Dimension("scope", Outcome.FULL, "实操范围完全覆盖目标要求")
    if source.practical_scope & target.practical_scope:
        return Dimension(
            "scope", Outcome.CONDITIONAL,
            "需补充实操项目: " + ", ".join(missing),
        )
    return Dimension(
        "scope", Outcome.NONE,
        "实操范围与目标无交集，缺少: " + ", ".join(missing),
    )


def _level_dimension(source: UnitFacts, target: UnitFacts, waived: bool) -> Dimension:
    if waived:
        return Dimension("level", Outcome.FULL, "等级差距已被生效例外豁免")
    gap = abs(source.level - target.level)
    if gap == 0:
        return Dimension("level", Outcome.FULL, f"等级一致（{source.level} 级）")
    if gap <= MAX_CONDITIONAL_LEVEL_GAP:
        return Dimension(
            "level", Outcome.CONDITIONAL,
            f"等级相差 {gap} 级（源 {source.level} / 目标 {target.level}），需补充评估",
        )
    return Dimension(
        "level", Outcome.NONE,
        f"等级相差 {gap} 级（源 {source.level} / 目标 {target.level}），超出可互认范围",
    )


def decide(
    source: UnitFacts,
    target: UnitFacts,
    active_exceptions: tuple[ActiveException, ...] = (),
) -> DecisionResult:
    """计算 source 持证方在 target 体系下的互认结论。"""
    kinds = {exc.kind for exc in active_exceptions}
    codes = tuple(sorted(exc.code for exc in active_exceptions))

    if ExceptionKind.FORCE_FULL in kinds:
        return DecisionResult(
            outcome=Outcome.FULL,
            conditions=(),
            dimensions=(Dimension("exception", Outcome.FULL, "生效例外强制完全互认"),),
            applied_exceptions=codes,
        )

    dimensions = (
        _hours_dimension(source, target, ExceptionKind.WAIVE_HOURS in kinds),
        _scope_dimension(source, target, ExceptionKind.WAIVE_SCOPE in kinds),
        _level_dimension(source, target, ExceptionKind.WAIVE_LEVEL in kinds),
    )
    blocked = tuple(d.note for d in dimensions if d.verdict == Outcome.NONE)
    if blocked:
        return DecisionResult(Outcome.NONE, blocked, dimensions, codes)
    conditions = tuple(d.note for d in dimensions if d.verdict == Outcome.CONDITIONAL)
    if conditions:
        return DecisionResult(Outcome.CONDITIONAL, conditions, dimensions, codes)
    return DecisionResult(Outcome.FULL, (), dimensions, codes)
