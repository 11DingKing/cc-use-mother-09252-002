"""循环映射：互认链查找必须在环上终止，且不被环污染结果。"""
from testkit import ServiceTestCase, unit_payload


class CyclicMappingTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.actors = self.make_standard_actors()
        # 三个国家的同构单元：A(CN)、B(DE)、C(JP)
        jp_actor = self.make_actor("jp-auth", ["authority"], ["authority:JP"])
        self.actors["jp"] = jp_actor
        self.unit_a = self.import_and_publish(
            self.actors["cn"], "CN-CYC", "CN", "1.0", [unit_payload("A")])["units"][0]["id"]
        self.unit_b = self.import_and_publish(
            self.actors["de"], "DE-CYC", "DE", "1.0", [unit_payload("B")])["units"][0]["id"]
        self.unit_c = self.import_and_publish(
            jp_actor, "JP-CYC", "JP", "1.0", [unit_payload("C")])["units"][0]["id"]
        # 构成环：A-B、B-C、C-A
        self.service.create_mapping(self.actors["cn"], self.unit_a, self.unit_b)
        self.service.create_mapping(self.actors["de"], self.unit_b, self.unit_c)
        self.service.create_mapping(self.actors["cn"], self.unit_c, self.unit_a)

    def test_cycle_terminates_and_finds_path(self):
        result = self.service.recognition_path(
            self.actors["cn"], self.unit_a, self.unit_c)
        self.assertTrue(result["found"])
        # 路径不含重复节点（环被访问集截断）
        self.assertEqual(len(result["path"]), len(set(result["path"])))
        self.assertEqual(result["path"][0], self.unit_a)
        self.assertEqual(result["path"][-1], self.unit_c)

    def test_cycle_reverse_direction(self):
        result = self.service.recognition_path(
            self.actors["cn"], self.unit_c, self.unit_b)
        self.assertTrue(result["found"])
        self.assertEqual(result["path"][0], self.unit_c)
        self.assertEqual(result["path"][-1], self.unit_b)

    def test_unreachable_when_edge_is_none(self):
        # 新增一个等级差 2 级的孤立单元 D：与 A 的映射为不可互认
        unit_d = self.import_and_publish(
            self.actors["cn"], "CN-ISO", "CN", "1.0",
            [unit_payload("D", level=5)])["units"][0]["id"]
        self.service.create_mapping(self.actors["cn"], self.unit_a, unit_d)
        result = self.service.recognition_path(
            self.actors["cn"], unit_d, self.unit_b)
        self.assertFalse(result["found"])
        self.assertEqual(result["path"], [])

    def test_revoked_mapping_breaks_cycle(self):
        # 撤销 C-A 与 B-C 后，A 到 C 不再可达
        mappings = self.service.trace_mapping(
            self.actors["cn"],
            self._mapping_id_of(self.unit_c, self.unit_a))["mapping"]
        self.service.revoke_mapping(self.actors["registry"], mappings["id"])
        m_bc = self._mapping_id_of(self.unit_b, self.unit_c)
        self.service.revoke_mapping(self.actors["registry"], m_bc)
        result = self.service.recognition_path(
            self.actors["cn"], self.unit_a, self.unit_c)
        self.assertFalse(result["found"])

    def _mapping_id_of(self, unit_x, unit_y):
        # 通过单元维度查映射 id（测试辅助）
        rows = self.container.service._repo.list_mappings_of_unit(unit_x)
        for row in rows:
            if row["unit_a_id"] == unit_y or row["unit_b_id"] == unit_y:
                return row["id"]
        raise AssertionError("映射不存在")


if __name__ == "__main__":
    import unittest
    unittest.main()
