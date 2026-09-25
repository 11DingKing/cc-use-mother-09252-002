"""共享测试设施：临时数据库、可变时钟与常用搭建助手。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from service_09252_002.clock import MutableClock
from service_09252_002.domain import Actor, Role
from service_09252_002.services import RecognitionService
from service_09252_002.storage import Database

BASE_TIME = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)

IMPORTER = Actor("import-bot", frozenset({Role.IMPORTER}))
MAPPER = Actor("mapper-1", frozenset({Role.MAPPER}))
EXPERT = Actor("expert-1", frozenset({Role.EXPERT}))
ADMIN = Actor("admin-1", frozenset({Role.ADMIN}))
APPROVER_A = Actor("approver-a", frozenset({Role.APPROVER}))
APPROVER_B = Actor("approver-b", frozenset({Role.APPROVER}))
ASSESSOR = Actor("assessor-1", frozenset({Role.MAPPER}))


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(BASE_TIME)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "recognition.db")
    yield database
    database.close()


@pytest.fixture
def service(db, clock) -> RecognitionService:
    return RecognitionService(db, clock)


def make_unit_payload(code: str, level: int = 3, hours: float = 100.0,
                      scope=("焊接",), evidence=None) -> dict:
    return {
        "code": code,
        "title": f"单元-{code}",
        "level": level,
        "credit_hours": hours,
        "practical_scope": list(scope),
        "evidence": evidence if evidence is not None else [
            {"kind": "实操考核", "description": f"{code} 实操记录", "mandatory": True}
        ],
    }


def import_standard(service: RecognitionService, code: str, authority: str,
                    units: list[dict], label: str = "1.0",
                    country: str = "CN") -> dict:
    """导入一个标准并返回导入结果。"""
    return service.import_standard(IMPORTER, {
        "code": code, "title": f"标准-{code}", "country": country,
        "authority": authority,
        "version": {"label": label, "units": units},
    })


def unit_id(db: Database, version_id: str, code: str) -> str:
    for unit in db.list_units(version_id):
        if unit["code"] == code:
            return unit["id"]
    raise AssertionError(f"单元不存在: {code}")


def approved_mapping(service: RecognitionService, source_unit: str, target_unit: str,
                     party_a: str, party_b: str) -> str:
    """创建映射并完成双方会签，返回映射 ID。"""
    mapping_id = service.create_mapping(MAPPER, {
        "source_unit_id": source_unit, "target_unit_id": target_unit,
        "rationale": "课程内容对应",
    })["mapping_id"]
    service.approve(APPROVER_A, "mapping", mapping_id, party_a)
    service.approve(APPROVER_B, "mapping", mapping_id, party_b)
    return mapping_id
