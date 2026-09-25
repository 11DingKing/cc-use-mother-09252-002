"""例外与会签：多方权限、幂等重放、拒绝与撤销。"""
from service_09252_002.services import (
    ConflictError,
    ForbiddenError,
    ValidationError,
)
from testkit import ServiceTestCase


class ExceptionApprovalTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.actors = self.make_standard_actors()
        self.cn_unit, self.de_unit = self.make_pair(self.actors)
        self.mapping_id = self.make_mapping(
            self.actors, self.cn_unit, self.de_unit)["id"]
        self.exc = self._propose()

    def _propose(self, **overrides):
        payload = {
            "mapping_id": self.mapping_id,
            "direction": "BOTH",
            "effect": "full",
            "reason": "试点期互认",
            "valid_from": "2026-08-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
        }
        payload.update(overrides)
        return self.service.propose_exception(self.actors["expert"], payload)

    # ---- 提案权限与校验 ----
    def test_propose_requires_expert_role(self):
        with self.assertRaises(ForbiddenError):
            self.service.propose_exception(self.actors["cn"], {
                "mapping_id": self.mapping_id, "direction": "BOTH", "effect": "full",
                "reason": "x", "valid_from": "2026-08-01T00:00:00Z",
                "valid_until": "2027-01-01T00:00:00Z",
            })

    def test_required_parties_snapshot(self):
        self.assertEqual(self.exc["required_parties"],
                         ["authority:CN", "authority:DE", "registry"])
        self.assertEqual(self.exc["status"], "proposed")
        self.assertEqual(self.exc["remaining_parties"],
                         ["authority:CN", "authority:DE", "registry"])

    def test_conditional_effect_requires_conditions(self):
        with self.assertRaises(ValidationError):
            self._propose(effect="conditional", conditions=[])

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValidationError):
            self._propose(valid_from="2027-01-01T00:00:00Z",
                          valid_until="2026-08-01T00:00:00Z")

    # ---- 会签权限 ----
    def test_approval_requires_party_membership(self):
        with self.assertRaises(ForbiddenError):
            self.service.approve_exception(
                self.actors["cn"], self.exc["id"], party="authority:DE",
                decision="approve", idempotency_key="k1")
        with self.assertRaises(ForbiddenError):
            self.service.approve_exception(
                self.actors["expert"], self.exc["id"], party="registry",
                decision="approve", idempotency_key="k2")

    def test_unknown_party_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.approve_exception(
                self.admin, self.exc["id"], party="authority:FR",
                decision="approve", idempotency_key="k1")

    # ---- 幂等 ----
    def test_idempotent_replay_returns_same_record(self):
        first, created = self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="key-cn-1")
        self.assertTrue(created)
        replay, created = self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="key-cn-1")
        self.assertFalse(created)
        self.assertEqual(replay["id"], first["id"])
        # 仍只有一条会签记录
        view = self.service.get_exception(self.actors["cn"], self.exc["id"])
        self.assertEqual(len(view["approvals"]), 1)

    def test_same_key_different_content_conflicts(self):
        self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="key-cn-1")
        with self.assertRaises(ConflictError):
            self.service.approve_exception(
                self.actors["cn"], self.exc["id"], party="authority:CN",
                decision="reject", idempotency_key="key-cn-1")

    def test_same_party_different_key_conflicts(self):
        self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="key-cn-1")
        with self.assertRaises(ConflictError):
            self.service.approve_exception(
                self.actors["cn"], self.exc["id"], party="authority:CN",
                decision="approve", idempotency_key="key-cn-2")

    # ---- 多方会签完成 ----
    def test_full_activation_flow(self):
        self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="k-cn")
        mid = self.service.get_exception(self.actors["cn"], self.exc["id"])
        self.assertEqual(mid["status"], "proposed")
        self.assertEqual(mid["remaining_parties"], ["authority:DE", "registry"])

        self.service.approve_exception(
            self.actors["de"], self.exc["id"], party="authority:DE",
            decision="approve", idempotency_key="k-de")
        self.service.approve_exception(
            self.actors["registry"], self.exc["id"], party="registry",
            decision="approve", idempotency_key="k-reg")

        view = self.service.get_exception(self.actors["cn"], self.exc["id"])
        self.assertEqual(view["status"], "approved")
        self.assertEqual(view["effective_status"], "effective")
        self.assertIsNotNone(view["decided_at"])
        self.assertEqual(view["remaining_parties"], [])

        # 生效中的例外覆盖规则结论
        mapping = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertEqual(mapping["mutual_outcome"], "full")
        self.assertTrue(mapping["directions"]["A_TO_B"]["source"].startswith("exception:"))

    def test_approval_after_close_conflicts_but_replay_ok(self):
        self.approve_all(self.actors, self.exc["id"])
        with self.assertRaises(ConflictError):
            self.service.approve_exception(
                self.actors["cn"], self.exc["id"], party="authority:CN",
                decision="approve", idempotency_key="brand-new-key")
        # 既有键重放仍然安全
        _, created = self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="k-authority:CN")
        self.assertFalse(created)

    # ---- 拒绝 ----
    def test_reject_path(self):
        self.service.approve_exception(
            self.actors["cn"], self.exc["id"], party="authority:CN",
            decision="approve", idempotency_key="k-cn")
        self.service.approve_exception(
            self.actors["registry"], self.exc["id"], party="registry",
            decision="reject", idempotency_key="k-reg")
        view = self.service.get_exception(self.actors["cn"], self.exc["id"])
        self.assertEqual(view["status"], "rejected")
        with self.assertRaises(ConflictError):
            self.service.approve_exception(
                self.actors["de"], self.exc["id"], party="authority:DE",
                decision="approve", idempotency_key="k-de")

    # ---- 撤销 ----
    def test_proposer_can_withdraw_proposed(self):
        view = self.service.revoke_exception(
            self.actors["expert"], self.exc["id"], reason="撤回")
        self.assertEqual(view["status"], "revoked")
        # 重复撤销幂等
        again = self.service.revoke_exception(self.actors["expert"], self.exc["id"])
        self.assertEqual(again["status"], "revoked")

    def test_non_registry_cannot_revoke_approved(self):
        self.approve_all(self.actors, self.exc["id"])
        with self.assertRaises(ForbiddenError):
            self.service.revoke_exception(self.actors["expert"], self.exc["id"])

    def test_registry_revokes_approved_and_mapping_falls_back(self):
        self.approve_all(self.actors, self.exc["id"])
        before = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertEqual(before["mutual_outcome"], "full")

        view = self.service.revoke_exception(
            self.actors["registry"], self.exc["id"], reason="试点结束")
        self.assertEqual(view["status"], "revoked")

        after = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertTrue(after["directions"]["A_TO_B"]["source"].startswith("rules:"))

    def test_revoke_rejected_conflicts(self):
        self.service.approve_exception(
            self.actors["registry"], self.exc["id"], party="registry",
            decision="reject", idempotency_key="k-reg")
        with self.assertRaises(ConflictError):
            self.service.revoke_exception(self.actors["registry"], self.exc["id"])

    # ---- 追溯 ----
    def test_trace_contains_full_history(self):
        self.approve_all(self.actors, self.exc["id"])
        trace = self.service.trace_mapping(self.actors["cn"], self.mapping_id)
        self.assertEqual(len(trace["decisions"]), 2)
        self.assertEqual(len(trace["exceptions"]), 1)
        self.assertEqual(len(trace["exceptions"][0]["approvals"]), 3)
        actions = [e["action"] for e in trace["events"]]
        self.assertIn("mapping.created", actions)
        self.assertIn("exception.proposed", actions)
        self.assertIn("exception.cosigned", actions)
        self.assertIn("exception.activated", actions)


if __name__ == "__main__":
    import unittest
    unittest.main()
