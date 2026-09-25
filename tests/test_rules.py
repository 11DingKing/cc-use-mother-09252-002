"""互认规则的纯函数测试：完全 / 附条件 / 不可互认的判定边界。"""
import unittest

from service_09252_002 import domain
from service_09252_002.domain import UnitProfile, evaluate_direction, worse


def profile(unit_id="u1", *, hours=100, level=3, scope=("arc", "safety"),
            evidence=("exam",)):
    return UnitProfile(
        unit_id=unit_id, code=unit_id, title=unit_id, hours=hours, level=level,
        practical_scope=frozenset(scope), mandatory_evidence=frozenset(evidence),
    )


class RuleTests(unittest.TestCase):
    def test_full_alignment(self):
        result = evaluate_direction(profile(), profile("u2"))
        self.assertEqual(result.outcome, domain.OUTCOME_FULL)
        self.assertEqual(result.conditions, ())

    def test_source_exceeds_target_is_full(self):
        result = evaluate_direction(
            profile(hours=160, scope=("arc", "safety", "extra")),
            profile("u2", hours=100),
        )
        self.assertEqual(result.outcome, domain.OUTCOME_FULL)

    def test_conditional_hours_gap(self):
        result = evaluate_direction(profile(hours=100), profile("u2", hours=120))
        self.assertEqual(result.outcome, domain.OUTCOME_CONDITIONAL)
        self.assertTrue(any("学时" in c for c in result.conditions))

    def test_none_when_hours_far_below(self):
        result = evaluate_direction(profile(hours=50), profile("u2", hours=100))
        self.assertEqual(result.outcome, domain.OUTCOME_NONE)

    def test_hours_boundary_ratio_is_conditional(self):
        # 恰好 80% 属于附条件，不是不可互认
        result = evaluate_direction(profile(hours=80), profile("u2", hours=100))
        self.assertEqual(result.outcome, domain.OUTCOME_CONDITIONAL)

    def test_conditional_level_gap_one(self):
        result = evaluate_direction(profile(level=3), profile("u2", level=4))
        self.assertEqual(result.outcome, domain.OUTCOME_CONDITIONAL)

    def test_none_when_level_gap_two(self):
        result = evaluate_direction(profile(level=3), profile("u2", level=5))
        self.assertEqual(result.outcome, domain.OUTCOME_NONE)

    def test_conditional_scope_gap(self):
        result = evaluate_direction(
            profile(scope=("arc", "safety")),
            profile("u2", scope=("arc", "safety", "laser")),
        )
        self.assertEqual(result.outcome, domain.OUTCOME_CONDITIONAL)
        self.assertTrue(any("laser" in c for c in result.conditions))

    def test_none_when_scope_coverage_low(self):
        result = evaluate_direction(
            profile(scope=("arc",)),
            profile("u2", scope=("arc", "laser", "plasma", "tig", "mig")),
        )
        self.assertEqual(result.outcome, domain.OUTCOME_NONE)

    def test_conditional_missing_evidence(self):
        result = evaluate_direction(
            profile(evidence=("exam",)),
            profile("u2", evidence=("exam", "portfolio")),
        )
        self.assertEqual(result.outcome, domain.OUTCOME_CONDITIONAL)

    def test_none_when_many_evidence_missing(self):
        result = evaluate_direction(
            profile(evidence=("exam",)),
            profile("u2", evidence=("exam", "portfolio", "demo", "logbook")),
        )
        self.assertEqual(result.outcome, domain.OUTCOME_NONE)

    def test_conditions_accumulate(self):
        result = evaluate_direction(
            profile(hours=100, level=3),
            profile("u2", hours=110, level=4),
        )
        self.assertEqual(result.outcome, domain.OUTCOME_CONDITIONAL)
        self.assertEqual(len(result.conditions), 2)

    def test_worse_ordering(self):
        self.assertEqual(worse(domain.OUTCOME_FULL, domain.OUTCOME_CONDITIONAL),
                         domain.OUTCOME_CONDITIONAL)
        self.assertEqual(worse(domain.OUTCOME_CONDITIONAL, domain.OUTCOME_NONE),
                         domain.OUTCOME_NONE)
        self.assertEqual(worse(domain.OUTCOME_FULL, domain.OUTCOME_FULL),
                         domain.OUTCOME_FULL)


if __name__ == "__main__":
    unittest.main()
