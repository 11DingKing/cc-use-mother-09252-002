"""测试共享工具：内存级服务装配与常用数据构造。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from service_09252_002.app import build_service
from service_09252_002.ports import FixedClock, SequentialIds, parse_instant

ADMIN_TOKEN = "test-admin-token"
START = "2026-09-01T00:00:00Z"


def unit_payload(code, *, hours=100, level=3, scope=("arc", "safety"),
                 evidence=("exam",)):
    return {
        "code": code,
        "title": f"能力单元 {code}",
        "hours": hours,
        "level": level,
        "practical_scope": list(scope),
        "evidence": [
            {"kind": kind, "detail": f"{kind} 证明", "mandatory": True}
            for kind in evidence
        ],
    }


def import_payload(code, country, label, units, *, title=None, parent=None,
                   effective_from="2026-01-01T00:00:00Z"):
    return {
        "standard": {
            "code": code,
            "title": title or f"{country} 职业技能标准 {code}",
            "country": country,
            "issuing_body": f"{country} 技能标准局",
        },
        "version": {
            "label": label,
            "parent_version_id": parent,
            "effective_from": effective_from,
        },
        "units": units,
    }


class ServiceTestCase(unittest.TestCase):
    """每个用例一套独立临时库 + 固定时钟 + 序列 ID。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "test.db")
        self.clock = FixedClock(parse_instant(START))
        self.container = build_service(
            self.db_path, clock=self.clock, id_generator=SequentialIds(),
            admin_token=ADMIN_TOKEN,
        )
        self.addCleanup(self.container.close)
        self.service = self.container.service
        self.admin = self.service.authenticate(ADMIN_TOKEN)

    # ---- 参与者 ----
    def make_actor(self, name, roles, parties):
        token = f"token-{name}-0123456789"
        self.service.create_actor(self.admin, name=name, token=token,
                                  roles=roles, parties=parties)
        return self.service.authenticate(token)

    def make_standard_actors(self):
        """常用四方：两国主管、登记方、专家。"""
        return {
            "cn": self.make_actor("cn-auth", ["authority"], ["authority:CN"]),
            "de": self.make_actor("de-auth", ["authority"], ["authority:DE"]),
            "registry": self.make_actor("registry", ["registry"], ["registry"]),
            "expert": self.make_actor("expert", ["expert"], []),
        }

    # ---- 数据构造 ----
    def import_and_publish(self, caller, code, country, label, units, **kwargs):
        view, _ = self.service.import_standard(
            caller, import_payload(code, country, label, units, **kwargs))
        self.service.publish_version(caller, view["version"]["id"])
        return view

    def make_pair(self, actors, *, cn_unit=None, de_unit=None, suffix="1"):
        """建一对已发布的中德单元，返回 (cn_unit_id, de_unit_id)。"""
        cn_view = self.import_and_publish(
            actors["cn"], f"CN-STD-{suffix}", "CN", "1.0",
            [cn_unit or unit_payload(f"CN-U{suffix}")])
        de_view = self.import_and_publish(
            actors["de"], f"DE-STD-{suffix}", "DE", "1.0",
            [de_unit or unit_payload(f"DE-U{suffix}")])
        return cn_view["units"][0]["id"], de_view["units"][0]["id"]

    def make_mapping(self, actors, cn_unit_id, de_unit_id):
        view, created = self.service.create_mapping(actors["cn"], cn_unit_id, de_unit_id)
        assert created
        return view

    def approve_all(self, actors, exception_id, key_prefix="k"):
        """三方会签全部同意。"""
        for party, actor in (("authority:CN", actors["cn"]),
                             ("authority:DE", actors["de"]),
                             ("registry", actors["registry"])):
            self.service.approve_exception(
                actor, exception_id, party=party, decision="approve",
                idempotency_key=f"{key_prefix}-{party}")
