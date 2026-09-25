"""规则引擎单元测试：学时、实操范围、等级三个维度的组合与例外宽限。"""
from __future__ import annotations

from service_09252_002.domain import ActiveException, ExceptionKind, Outcome, UnitFacts
from service_09252_002.rules import RULE_VERSION, decide


def facts(uid: str, level: int = 3, hours: float = 100.0,
          scope=("焊接", "切割")) -> UnitFacts:
    return UnitFacts(id=uid, code=uid, title=uid, level=level,
                     credit_hours=hours, practical_scope=frozenset(scope))


def exception(kind: str, code: str = "EX-1") -> ActiveException:
    return ActiveException(id="e1", code=code, kind=kind, reason="测试")


def test_full_recognition_when_all_dimensions_satisfied():
    result = decide(facts("s", hours=120.0, scope=("焊接", "切割", "质检")),
                    facts("t", hours=100.0))
    assert result.outcome == Outcome.FULL
    assert result.conditions == ()
    assert result.rule_version == RULE_VERSION


def test_conditional_when_hours_gap_is_bridgeable():
    result = decide(facts("s", hours=75.0), facts("t", hours=100.0))
    assert result.outcome == Outcome.CONDITIONAL
    assert any("补足学时" in c and "25" in c for c in result.conditions)


def test_conditional_when_scope_partially_missing():
    result = decide(facts("s", scope=("焊接",)), facts("t", scope=("焊接", "切割")))
    assert result.outcome == Outcome.CONDITIONAL
    assert any("切割" in c for c in result.conditions)


def test_conditional_when_level_gap_is_one():
    result = decide(facts("s", level=2), facts("t", level=3))
    assert result.outcome == Outcome.CONDITIONAL
    assert any("等级" in c for c in result.conditions)


def test_none_when_hours_ratio_too_low():
    result = decide(facts("s", hours=50.0), facts("t", hours=100.0))
    assert result.outcome == Outcome.NONE
    assert any("学时" in c for c in result.conditions)


def test_none_when_scope_disjoint():
    result = decide(facts("s", scope=("烹饪",)), facts("t", scope=("焊接",)))
    assert result.outcome == Outcome.NONE


def test_none_when_level_gap_too_large():
    result = decide(facts("s", level=1), facts("t", level=3))
    assert result.outcome == Outcome.NONE


def test_single_blocking_dimension_forces_none():
    result = decide(facts("s", hours=200.0, level=1), facts("t", level=3))
    assert result.outcome == Outcome.NONE


def test_waive_hours_exception_lifts_hours_block():
    result = decide(facts("s", hours=40.0), facts("t", hours=100.0),
                    (exception(ExceptionKind.WAIVE_HOURS),))
    assert result.outcome == Outcome.FULL
    assert result.applied_exceptions == ("EX-1",)


def test_waive_scope_exception_covers_missing_items():
    result = decide(facts("s", scope=()), facts("t", scope=("焊接",)),
                    (exception(ExceptionKind.WAIVE_SCOPE),))
    assert result.outcome == Outcome.FULL


def test_force_full_exception_overrides_everything():
    result = decide(facts("s", level=1, hours=10.0, scope=()),
                    facts("t", level=5, hours=500.0, scope=("焊接",)),
                    (exception(ExceptionKind.FORCE_FULL, "EX-F"),))
    assert result.outcome == Outcome.FULL
    assert result.applied_exceptions == ("EX-F",)


def test_hours_boundary_ratios():
    # 0.9 恰好完全互认，0.6 恰好附条件，低于 0.6 不可互认
    assert decide(facts("s", hours=90.0), facts("t")).outcome == Outcome.FULL
    assert decide(facts("s", hours=60.0), facts("t")).outcome == Outcome.CONDITIONAL
    assert decide(facts("s", hours=59.9), facts("t")).outcome == Outcome.NONE
