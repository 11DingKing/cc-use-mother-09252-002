"""重启一致性：SQLite 数据在服务重建后保持完整，流程可继续。"""
import tempfile
import unittest
from pathlib import Path

from service_09252_002.app import build_service
from service_09252_002.ports import FixedClock, parse_instant
from testkit import ADMIN_TOKEN, START, ServiceTestCase


class PersistenceTests(ServiceTestCase):
    def test_restart_consistency(self):
        actors = self.make_standard_actors()
        cn_unit, de_unit = self.make_pair(actors)
        mapping = self.make_mapping(actors, cn_unit, de_unit)
        mapping_id = mapping["id"]
        exc = self.service.propose_exception(actors["expert"], {
            "mapping_id": mapping_id,
            "direction": "BOTH",
            "effect": "full",
            "reason": "重启前提出",
            "valid_from": "2026-08-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
        })
        # 重启前完成两方会签
        self.service.approve_exception(
            actors["cn"], exc["id"], party="authority:CN",
            decision="approve", idempotency_key="persist-cn")
        self.service.approve_exception(
            actors["de"], exc["id"], party="authority:DE",
            decision="approve", idempotency_key="persist-de")

        snapshot_mapping = self.service.get_mapping_view(actors["cn"], mapping_id)
        snapshot_exc = self.service.get_exception(actors["cn"], exc["id"])
        snapshot_trace = self.service.trace_mapping(actors["cn"], mapping_id)
        self.container.close()

        # 同一数据库文件重建服务（模拟重启）
        clock = FixedClock(parse_instant(START))
        reopened = build_service(self.db_path, clock=clock, admin_token=ADMIN_TOKEN)
        self.addCleanup(reopened.close)
        service2 = reopened.service

        # 引导管理员幂等：同名管理员不重复创建，令牌仍可用
        admin2 = service2.authenticate(ADMIN_TOKEN)
        self.assertEqual(admin2.name, "bootstrap-admin")

        cn2 = service2.authenticate("token-cn-auth-0123456789")
        mapping2 = service2.get_mapping_view(cn2, mapping_id)
        self.assertEqual(mapping2, snapshot_mapping)

        exc2 = service2.get_exception(cn2, exc["id"])
        self.assertEqual(exc2, snapshot_exc)
        self.assertEqual(exc2["status"], "proposed")
        self.assertEqual(len(exc2["approvals"]), 2)

        trace2 = service2.trace_mapping(cn2, mapping_id)
        self.assertEqual(trace2, snapshot_trace)

        # 流程可继续：重启后完成第三方会签，例外激活
        registry2 = service2.authenticate("token-registry-0123456789")
        _, created = service2.approve_exception(
            registry2, exc["id"], party="registry",
            decision="approve", idempotency_key="persist-reg")
        self.assertTrue(created)
        final = service2.get_exception(cn2, exc["id"])
        self.assertEqual(final["status"], "approved")
        self.assertEqual(final["effective_status"], "effective")

        # 重启前的会签幂等键仍然有效（重放不重复计数）
        _, created = service2.approve_exception(
            service2.authenticate("token-cn-auth-0123456789"), exc["id"],
            party="authority:CN", decision="approve",
            idempotency_key="persist-cn")
        self.assertFalse(created)
        self.assertEqual(len(service2.get_exception(cn2, exc["id"])["approvals"]), 3)

    def test_wal_files_do_not_leak_into_source_tree(self):
        # 运行数据只出现在临时目录，源码目录不产生数据文件
        self.make_standard_actors()
        workspace = Path(__file__).resolve().parent.parent
        stray = [p for p in workspace.rglob("*.db*") if p.is_file()]
        self.assertEqual(stray, [])


if __name__ == "__main__":
    unittest.main()
