"""领域模型与互认规则。

规则是纯函数：输入两个能力单元画像，输出单向评估结果；
双向映射的互认结论取两个方向中较差者。规则版本号 RULES_VERSION
随每条决定一并持久化，保证历史决定可解释、不可改写。
"""
from __future__ import annotations

from dataclasses import dataclass

OUTCOME_FULL = "full"
OUTCOME_CONDITIONAL = "conditional"
OUTCOME_NONE = "none"
OUTCOMES = (OUTCOME_FULL, OUTCOME_CONDITIONAL, OUTCOME_NONE)

# 数值越小结论越差；双向互认取较差方向
_OUTCOME_RANK = {OUTCOME_NONE: 0, OUTCOME_CONDITIONAL: 1, OUTCOME_FULL: 2}

RULES_VERSION = "1.0.0"

# 规则阈值（集中声明，便于审计与调整）
HOURS_RATIO_FLOOR = 0.8        # 学时比低于该值判不可互认
LEVEL_GAP_TOLERANCE = 1        # 等级差超过该值判不可互认
SCOPE_COVERAGE_FLOOR = 0.6     # 实操范围覆盖率低于该值判不可互认
MISSING_EVIDENCE_TOLERANCE = 2  # 缺失必备证据超过该数量判不可互认

DIRECTION_A_TO_B = "A_TO_B"
DIRECTION_B_TO_A = "B_TO_A"
DIRECTION_BOTH = "BOTH"
DIRECTIONS = (DIRECTION_A_TO_B, DIRECTION_B_TO_A)


@dataclass(frozen=True)
class UnitProfile:
    """规则引擎使用的能力单元画像。"""

    unit_id: str
    code: str
    title: str
    hours: int
    level: int
    practical_scope: frozenset[str]
    mandatory_evidence: frozenset[str]


@dataclass(frozen=True)
class Evaluation:
    """单向评估结果：结论、附条件清单与理由。"""

    outcome: str
    conditions: tuple[str, ...]
    rationale: str


def worse(left: str, right: str) -> str:
    """取两个互认结论中较差者。"""
    return left if _OUTCOME_RANK[left] <= _OUTCOME_RANK[right] else right


def evaluate_direction(source: UnitProfile, target: UnitProfile) -> Evaluation:
    """评估 source 对 target 的互认程度（单向）。

    完全互认：等级相同、学时不低于目标、实操范围全覆盖、必备证据齐全；
    附条件互认：差异可弥补（等级差 1 级、学时缺口 ≤20%、范围覆盖 ≥60%、缺证据 ≤2 项）；
    不可互认：任一硬性差距超限。
    """
    conditions: list[str] = []
    failures: list[str] = []

    gap = abs(source.level - target.level)
    if gap > LEVEL_GAP_TOLERANCE:
        failures.append(
            f"等级相差 {gap} 级（{source.level} vs {target.level}），超过允许的 {LEVEL_GAP_TOLERANCE} 级"
        )
    elif gap == 1:
        conditions.append(
            f"等级相差 1 级（{source.level} vs {target.level}），需补充等级衔接评估"
        )

    if source.hours < target.hours:
        ratio = source.hours / target.hours if target.hours > 0 else 0.0
        if ratio < HOURS_RATIO_FLOOR:
            failures.append(
                f"学时 {source.hours} 不足目标 {target.hours} 的 {HOURS_RATIO_FLOOR:.0%}"
            )
        else:
            conditions.append(
                f"学时不足：{source.hours}/{target.hours}，需补足 {target.hours - source.hours} 学时"
            )

    if target.practical_scope:
        missing_scope = sorted(target.practical_scope - source.practical_scope)
        if missing_scope:
            coverage = (
                len(target.practical_scope & source.practical_scope)
                / len(target.practical_scope)
            )
            if coverage < SCOPE_COVERAGE_FLOOR:
                failures.append(
                    f"实操范围覆盖率 {coverage:.0%} 低于 {SCOPE_COVERAGE_FLOOR:.0%}，"
                    f"缺失：{'、'.join(missing_scope)}"
                )
            else:
                conditions.append(f"实操范围缺失：{'、'.join(missing_scope)}")

    missing_evidence = sorted(target.mandatory_evidence - source.mandatory_evidence)
    if len(missing_evidence) > MISSING_EVIDENCE_TOLERANCE:
        failures.append(
            f"缺失必备证据 {len(missing_evidence)} 项：{'、'.join(missing_evidence)}"
        )
    elif missing_evidence:
        conditions.append(f"需补充证据：{'、'.join(missing_evidence)}")

    if failures:
        return Evaluation(OUTCOME_NONE, tuple(conditions), "；".join(failures))
    if conditions:
        return Evaluation(OUTCOME_CONDITIONAL, tuple(conditions), "存在可弥补差异")
    return Evaluation(OUTCOME_FULL, (), "能力单元完全对齐")
