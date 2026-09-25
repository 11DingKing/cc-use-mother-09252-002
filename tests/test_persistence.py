"""重启一致性：SQLite 持久化状态在服务重启后完整可用，幂等键同样存活。"""
from __future__ import annotations

from service_09252_002.clock import MutableClock
from service_09252_002.domain import MappingStatus
from service_09252_002.services import RecognitionService
from service_09252_002.storage import Database
from tests.conftest import (
    ADMIN, BASE_TIME, IMPORTER, MAPPER,
    approved_mapping, import_standard, make_unit_payload, unit_id,
)

AUTH_S = "源国技能局"
AUTH_T = "目标国技能院"


def _build(path):
    clock = MutableClock(BASE_TIME)
    db = Database(path)
    service = RecognitionService(db, clock)
    s = import_standard(service, "STD-S", AUTH_S, [make_unit_payload("u1", hours=80.0)])
    t = import_standard(service, "STD-T", AUTH_T, [make_unit_payload("v1")])
    mapping_id = approved_mapping(service, unit_id(db, s["version_id"], "u1"),
                                  unit_id(db, t["version_id"], "v1"), AUTH_S, AUTH_T)
    service.compare_versions(MAPPER, s["version_id"], t["version_id"])
    service.import_standard(IMPORTER, {
        "code": "STD-KEY", "title": "幂等标准", "country": "CN", "authority": "某机构",
        "version": {"label": "1.0", "units": [make_unit_payload("k1")]},
    }, idempotency_key="RESTART-K")
    db.close()
    return mapping_id, s, t


def test_state_survives_restart(tmp_path):
    path = tmp_path / "restart.db"
    mapping_id, s, t = _build(path)

    db2 = Database(path)
    service2 = RecognitionService(db2, MutableClock(BASE_TIME))
    mapping = db2.get_mapping(mapping_id)
    assert mapping["status"] == MappingStatus.APPROVED

    trace = service2.trace_mapping(MAPPER, mapping_id)
    assert len(trace["decisions"]) == 2
    assert len(trace["approvals"]) == 2
    assert [e["action"] for e in trace["events"]] == [
        "created", "party_approved", "party_approved", "approved",
    ]
    # 重启后仍可继续业务流程：撤销并追溯
    service2.revoke(ADMIN, "mapping", mapping_id, "重启后撤销")
    assert db2.get_mapping(mapping_id)["status"] == MappingStatus.REVOKED
    db2.close()

    # 第三次打开：撤销状态仍在
    db3 = Database(path)
    assert db3.get_mapping(mapping_id)["status"] == MappingStatus.REVOKED
    db3.close()


def test_idempotency_keys_survive_restart(tmp_path):
    path = tmp_path / "restart-idem.db"
    _build(path)
    db2 = Database(path)
    service2 = RecognitionService(db2, MutableClock(BASE_TIME))
    replay = service2.import_standard(IMPORTER, {
        "code": "STD-KEY", "title": "幂等标准", "country": "CN", "authority": "某机构",
        "version": {"label": "1.0", "units": [make_unit_payload("k1")]},
    }, idempotency_key="RESTART-K")
    assert replay["meta"]["idempotent_replay"] is True
    # 标准没有被重复导入
    assert db2.find_standard_by_code("STD-KEY") is not None
    db2.close()
