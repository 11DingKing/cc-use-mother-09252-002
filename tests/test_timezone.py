"""跨时区生效：例外期限以任意时区表达，判定一律按 UTC 绝对时刻。"""
from __future__ import annotations

from datetime import datetime, timezone

from service_09252_002.domain import Outcome
from tests.conftest import (
    APPROVER_A, APPROVER_B, EXPERT, MAPPER,
    approved_mapping, import_standard, make_unit_payload, unit_id,
)

AUTH_S = "源国技能局"
AUTH_T = "目标国技能院"
UTC = timezone.utc


def _setup(service, db):
    """源单元 70 学时 vs 目标 100 学时：无例外时为附条件互认。"""
    s = import_standard(service, "STD-S", AUTH_S, [make_unit_payload("u1", hours=70.0)])
    t = import_standard(service, "STD-T", AUTH_T, [make_unit_payload("v1", hours=100.0)])
    approved_mapping(service, unit_id(db, s["version_id"], "u1"),
                     unit_id(db, t["version_id"], "v1"), AUTH_S, AUTH_T)
    return s, t


def _forward_outcome(service, s, t) -> str:
    report = service.compare_versions(MAPPER, s["version_id"], t["version_id"])
    for decision in report["results"][0]["decisions"]:
        if decision["from_version_id"] == s["version_id"]:
            return decision["outcome"]
    raise AssertionError("缺少正向决定")


def _approved_exception(service, code, effective_from, effective_until, version_id):
    exc = service.propose_exception(EXPERT, {
        "code": code, "version_id": version_id, "kind": "waive_hours",
        "reason": "联合培养项目覆盖学时差额",
        "counterparty_authority": AUTH_T,
        "effective_from": effective_from, "effective_until": effective_until,
    })
    service.approve(APPROVER_A, "exception", exc["exception_id"], AUTH_S)
    service.approve(APPROVER_B, "exception", exc["exception_id"], AUTH_T)
    return exc


def test_beijing_window_activates_at_utc_boundary(service, db, clock):
    s, t = _setup(service, db)
    # 北京时间 10-01 00:00 至 10-08 00:00，即 UTC 09-30 16:00 至 10-07 16:00
    exc = _approved_exception(service, "EX-BJ",
                              "2026-10-01T00:00:00+08:00",
                              "2026-10-08T00:00:00+08:00", s["version_id"])
    # 存储已归一到 UTC
    stored = db.get_exception(exc["exception_id"])
    assert stored["effective_from"] == "2026-09-30T16:00:00+00:00"
    assert stored["effective_until"] == "2026-10-07T16:00:00+00:00"

    clock.set(datetime(2026, 9, 30, 15, 59, 59, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL
    clock.set(datetime(2026, 9, 30, 16, 0, 0, tzinfo=UTC))  # 北京 10-01 00:00
    assert _forward_outcome(service, s, t) == Outcome.FULL
    clock.set(datetime(2026, 10, 7, 15, 59, 59, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.FULL
    clock.set(datetime(2026, 10, 7, 16, 0, 0, tzinfo=UTC))  # 北京 10-08 00:00，到期
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL


def test_same_window_in_other_timezone_behaves_identically(service, db, clock):
    s, t = _setup(service, db)
    # 同一绝对窗口用 UTC-5 表达
    _approved_exception(service, "EX-NY",
                        "2026-09-30T11:00:00-05:00",
                        "2026-10-07T11:00:00-05:00", s["version_id"])
    clock.set(datetime(2026, 9, 30, 15, 59, 59, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL
    clock.set(datetime(2026, 9, 30, 16, 0, 0, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.FULL
    clock.set(datetime(2026, 10, 7, 16, 0, 0, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL


def test_naive_timestamp_is_treated_as_utc(service, db, clock):
    s, t = _setup(service, db)
    _approved_exception(service, "EX-UTC", "2026-10-01T00:00:00",
                        "2026-10-08T00:00:00", s["version_id"])
    clock.set(datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL
    clock.set(datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.FULL


def test_unapproved_exception_has_no_effect(service, db, clock):
    s, t = _setup(service, db)
    service.propose_exception(EXPERT, {
        "code": "EX-PENDING", "version_id": s["version_id"], "kind": "waive_hours",
        "reason": "待会签", "counterparty_authority": AUTH_T,
        "effective_from": "2026-09-01T00:00:00+08:00",
        "effective_until": "2026-12-01T00:00:00+08:00",
    })
    clock.set(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL


def test_revoked_exception_stops_applying(service, db, clock):
    s, t = _setup(service, db)
    exc = _approved_exception(service, "EX-REV",
                              "2026-09-01T00:00:00Z", "2027-01-01T00:00:00Z",
                              s["version_id"])
    clock.set(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    assert _forward_outcome(service, s, t) == Outcome.FULL
    from tests.conftest import ADMIN
    service.revoke(ADMIN, "exception", exc["exception_id"], "提前终止宽限")
    assert _forward_outcome(service, s, t) == Outcome.CONDITIONAL
