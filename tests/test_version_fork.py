"""版本分叉：源标准更新标记受影响映射，历史决定不被改写。"""
from testkit import ServiceTestCase, import_payload, unit_payload


class VersionForkTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.actors = self.make_standard_actors()
        self.cn_unit, self.de_unit = self.make_pair(self.actors)
        self.mapping = self.make_mapping(self.actors, self.cn_unit, self.de_unit)
        self.mapping_id = self.mapping["id"]
        self.cn_version_id = self.mapping["unit_a" if self.mapping["unit_a"]["standard"]["country"] == "CN" else "unit_b"]["version"]["id"]

    def _import_cn_version(self, label, parent_id):
        view, _ = self.service.import_standard(
            self.actors["cn"],
            import_payload("CN-STD-1", "CN", label,
                           [unit_payload(f"CN-U-{label}")], parent=parent_id))
        return view["version"]["id"]

    def test_publish_marks_affected_and_preserves_history(self):
        before = self.service.trace_mapping(self.actors["cn"], self.mapping_id)
        self.assertEqual(self.mapping["status"], "active")

        v11 = self._import_cn_version("1.1", parent_id=self.cn_version_id)
        result = self.service.publish_version(self.actors["cn"], v11)
        self.assertIn(self.mapping_id, result["affected_mapping_ids"])

        view = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertEqual(view["status"], "affected")

        after = self.service.trace_mapping(self.actors["cn"], self.mapping_id)
        # 历史决定未被改写：条数与内容完全一致
        self.assertEqual(before["decisions"], after["decisions"])
        # 受影响事件已入审计
        actions = [e["action"] for e in after["events"]]
        self.assertIn("mapping.affected", actions)

    def test_forked_versions_both_mark_without_duplicates(self):
        v11 = self._import_cn_version("1.1", parent_id=self.cn_version_id)
        self.service.publish_version(self.actors["cn"], v11)
        # 分叉：v2.0 同样以 v1.0 为父版本
        v20 = self._import_cn_version("2.0", parent_id=self.cn_version_id)
        result = self.service.publish_version(self.actors["cn"], v20)
        # 映射已是 affected，不会重复标记
        self.assertNotIn(self.mapping_id, result["affected_mapping_ids"])
        view = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertEqual(view["status"], "affected")
        # 分叉的两个版本各自独立存在
        standard = self.service.get_standard_view(
            self.actors["cn"], self.mapping["unit_a"]["standard"]["id"]
            if self.mapping["unit_a"]["standard"]["country"] == "CN"
            else self.mapping["unit_b"]["standard"]["id"])
        labels = [v["label"] for v in standard["versions"]]
        self.assertEqual(sorted(labels), ["1.0", "1.1", "2.0"])

    def test_recompute_appends_without_rewriting(self):
        v11 = self._import_cn_version("1.1", parent_id=self.cn_version_id)
        self.service.publish_version(self.actors["cn"], v11)
        before = self.service.trace_mapping(self.actors["cn"], self.mapping_id)["decisions"]

        view = self.service.recompute_mapping(self.actors["cn"], self.mapping_id)
        self.assertEqual(view["status"], "active")

        after = self.service.trace_mapping(self.actors["cn"], self.mapping_id)["decisions"]
        self.assertEqual(len(after), len(before) + 2)  # 两个方向各追加一条
        self.assertEqual([d["id"] for d in before],
                         [d["id"] for d in after[:len(before)]])
        self.assertEqual({d["seq"] for d in after}, {1, 2})

    def test_exception_stays_bound_to_old_version(self):
        # 针对 v1.0 映射的例外生效后，发布新版本不影响旧映射上的例外
        exc = self.service.propose_exception(self.actors["expert"], {
            "mapping_id": self.mapping_id,
            "direction": "BOTH",
            "effect": "conditional",
            "conditions": ["补充 20 学时实操"],
            "reason": "两国主管机构试点互认",
            "valid_from": "2026-08-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
        })
        self.approve_all(self.actors, exc["id"])

        v11 = self._import_cn_version("1.1", parent_id=self.cn_version_id)
        self.service.publish_version(self.actors["cn"], v11)

        # 旧映射被标记受影响，但例外仍按其期限生效
        view = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertEqual(view["status"], "affected")
        self.assertEqual(view["directions"]["A_TO_B"]["outcome"], "conditional")
        self.assertTrue(view["directions"]["A_TO_B"]["source"].startswith("exception:"))

        # 新版本单元上的新映射不受旧例外影响
        new_cn_unit = self.service.get_version_view(self.actors["cn"], v11)["units"][0]["id"]
        new_mapping, _ = self.service.create_mapping(
            self.actors["cn"], new_cn_unit, self.de_unit)
        self.assertTrue(
            new_mapping["directions"]["A_TO_B"]["source"].startswith("rules:"))

    def test_retire_marks_mappings_affected(self):
        result = self.service.retire_version(self.actors["cn"], self.cn_version_id)
        self.assertIn(self.mapping_id, result["affected_mapping_ids"])
        view = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        self.assertEqual(view["status"], "affected")


if __name__ == "__main__":
    import unittest
    unittest.main()
