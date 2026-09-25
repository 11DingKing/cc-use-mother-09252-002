"""API 全流程：导入、比对、会签、撤销、追溯，以及权限与幂等约束。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from service_09252_002.api import create_app
from service_09252_002.clock import MutableClock

BASE_TIME = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
AUTH_S = "源国技能局"
AUTH_T = "目标国技能院"


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.db"), clock=MutableClock(BASE_TIME))
    app.config["TESTING"] = True
    return app.test_client()


def hdr(actor, *roles, key=None):
    headers = {"X-Actor": actor, "X-Roles": ",".join(roles)}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _standard_payload(code, authority, unit_code, hours=100.0):
    return {
        "code": code, "title": f"标准-{code}", "country": "CN", "authority": authority,
        "version": {"label": "1.0", "units": [{
            "code": unit_code, "title": f"单元-{unit_code}", "level": 3,
            "credit_hours": hours, "practical_scope": ["焊接"],
            "evidence": [{"kind": "实操考核", "description": "实操记录"}],
        }]},
    }


def _import(client, code, authority, unit_code, hours=100.0, key=None):
    return client.post("/standards/import", json=_standard_payload(code, authority, unit_code, hours),
                       headers=hdr("importer-1", "importer", key=key))


def _setup_pair(client):
    s = _import(client, "STD-S", AUTH_S, "u1", hours=80.0).get_json()
    t = _import(client, "STD-T", AUTH_T, "v1", hours=100.0).get_json()
    units_s = client.get(f"/versions/{s['version_id']}/units",
                         headers=hdr("mapper-1", "mapper")).get_json()["units"]
    units_t = client.get(f"/versions/{t['version_id']}/units",
                         headers=hdr("mapper-1", "mapper")).get_json()["units"]
    mapping_id = client.post("/mappings", json={
        "source_unit_id": units_s[0]["id"], "target_unit_id": units_t[0]["id"],
        "rationale": "课程内容对应",
    }, headers=hdr("mapper-1", "mapper")).get_json()["mapping_id"]
    return s, t, mapping_id


def _approve_both(client, subject_type, subject_id):
    client.post(f"/{subject_type}s/{subject_id}/approve", json={"party": AUTH_S},
                headers=hdr("approver-a", "approver"))
    return client.post(f"/{subject_type}s/{subject_id}/approve", json={"party": AUTH_T},
                       headers=hdr("approver-b", "approver"))


# ---------------------------------------------------------------- 权限

def test_missing_actor_header_is_rejected(client):
    resp = client.post("/standards/import", json=_standard_payload("X", "Y", "u"))
    assert resp.status_code == 403


def test_unknown_role_is_rejected(client):
    resp = client.post("/standards/import", json=_standard_payload("X", "Y", "u"),
                       headers=hdr("a", "superuser"))
    assert resp.status_code == 400


def test_import_requires_importer_role(client):
    resp = client.post("/standards/import", json=_standard_payload("X", "Y", "u"),
                       headers=hdr("a", "mapper"))
    assert resp.status_code == 403


def test_approve_requires_approver_role(client):
    _, _, mapping_id = _setup_pair(client)
    resp = client.post(f"/mappings/{mapping_id}/approve", json={"party": AUTH_S},
                       headers=hdr("mapper-1", "mapper"))
    assert resp.status_code == 403


def test_revoke_requires_admin_role(client):
    _, _, mapping_id = _setup_pair(client)
    resp = client.post(f"/mappings/{mapping_id}/revoke", json={"reason": "x"},
                       headers=hdr("approver-a", "approver"))
    assert resp.status_code == 403


# ---------------------------------------------------------------- 幂等

def test_import_idempotent_replay(client):
    first = _import(client, "STD-S", AUTH_S, "u1", key="K-1")
    assert first.status_code == 201
    replay = _import(client, "STD-S", AUTH_S, "u1", key="K-1")
    assert replay.status_code == 201
    assert replay.get_json()["meta"]["idempotent_replay"] is True
    assert replay.get_json()["standard_id"] == first.get_json()["standard_id"]
    # 换键重复导入同一标准 → 冲突
    conflict = _import(client, "STD-S", AUTH_S, "u1", key="K-2")
    assert conflict.status_code == 409


def test_idempotency_key_bound_to_actor_and_endpoint(client):
    _import(client, "STD-S", AUTH_S, "u1", key="K-9")
    other_actor = client.post("/standards/import",
                              json=_standard_payload("STD-Q", "Q", "q1"),
                              headers=hdr("importer-2", "importer", key="K-9"))
    assert other_actor.status_code == 409
    other_endpoint = client.post("/mappings", json={
        "source_unit_id": "a", "target_unit_id": "b",
    }, headers=hdr("importer-1", "importer,mapper", key="K-9"))
    assert other_endpoint.status_code == 409


def test_approve_is_naturally_idempotent(client):
    _, _, mapping_id = _setup_pair(client)
    first = client.post(f"/mappings/{mapping_id}/approve", json={"party": AUTH_S},
                        headers=hdr("approver-a", "approver"))
    assert first.status_code == 200
    again = client.post(f"/mappings/{mapping_id}/approve", json={"party": AUTH_S},
                        headers=hdr("approver-a", "approver"))
    assert again.status_code == 200
    assert "幂等" in again.get_json()["note"]
    assert len(again.get_json()["approvals"]) == 1


# ---------------------------------------------------------------- 会签

def test_multi_party_approval_flow(client):
    _, _, mapping_id = _setup_pair(client)
    # 非当事机构不能会签
    outsider = client.post(f"/mappings/{mapping_id}/approve", json={"party": "第三机构"},
                           headers=hdr("approver-x", "approver"))
    assert outsider.status_code == 403
    # 第一方签署后仍未生效
    half = client.post(f"/mappings/{mapping_id}/approve", json={"party": AUTH_S},
                       headers=hdr("approver-a", "approver")).get_json()
    assert half["status"] == "proposed"
    # 同一操作者不得代表第二方
    self_dealing = client.post(f"/mappings/{mapping_id}/approve", json={"party": AUTH_T},
                               headers=hdr("approver-a", "approver"))
    assert self_dealing.status_code == 403
    # 第二方另一位代表签署后生效
    done = client.post(f"/mappings/{mapping_id}/approve", json={"party": AUTH_T},
                       headers=hdr("approver-b", "approver")).get_json()
    assert done["status"] == "approved"
    assert sorted(done["signed_parties"]) == sorted([AUTH_S, AUTH_T])


# ---------------------------------------------------------------- 全流程

def test_full_lifecycle_compare_revoke_trace(client):
    s, t, mapping_id = _setup_pair(client)
    _approve_both(client, "mapping", mapping_id)
    # 比对：80/100 学时为附条件，双向各一条决定
    report = client.post("/comparisons", json={
        "version_a_id": s["version_id"], "version_b_id": t["version_id"],
    }, headers=hdr("mapper-1", "mapper")).get_json()
    outcomes = {d["from_version_id"]: d["outcome"]
                for d in report["results"][0]["decisions"]}
    assert outcomes[s["version_id"]] == "conditional"
    assert outcomes[t["version_id"]] == "full"
    # 撤销（幂等）
    revoked = client.post(f"/mappings/{mapping_id}/revoke", json={"reason": "标准换版"},
                          headers=hdr("admin-1", "admin")).get_json()
    assert revoked["status"] == "revoked"
    again = client.post(f"/mappings/{mapping_id}/revoke", json={"reason": "重复"},
                        headers=hdr("admin-1", "admin")).get_json()
    assert "幂等" in again["note"]
    # 撤销后不再参与比对
    report2 = client.post("/comparisons", json={
        "version_a_id": s["version_id"], "version_b_id": t["version_id"],
    }, headers=hdr("mapper-1", "mapper")).get_json()
    assert report2["results"] == []
    # 追溯：决定与会签历史完整保留
    trace = client.get(f"/mappings/{mapping_id}/trace",
                       headers=hdr("mapper-1", "mapper")).get_json()
    assert len(trace["decisions"]) == 2
    assert len(trace["approvals"]) == 2
    actions = [e["action"] for e in trace["events"]]
    assert actions == ["created", "party_approved", "party_approved",
                       "approved", "revoked"]
    assert trace["source"]["evidence"][0]["kind"] == "实操考核"


def test_exception_lifecycle_via_api(client):
    s, _, mapping_id = _setup_pair(client)
    _approve_both(client, "mapping", mapping_id)
    exc = client.post("/exceptions", json={
        "code": "EX-1", "version_id": s["version_id"], "kind": "waive_hours",
        "reason": "联合培养覆盖差额", "counterparty_authority": AUTH_T,
        "effective_from": "2026-09-01T00:00:00+08:00",
        "effective_until": "2027-09-01T00:00:00+08:00",
    }, headers=hdr("expert-1", "expert"))
    assert exc.status_code == 201
    exc_id = exc.get_json()["exception_id"]
    done = _approve_both(client, "exception", exc_id).get_json()
    assert done["status"] == "approved"
    # 例外生效后比对结果提升为完全互认
    report = client.post("/comparisons", json={
        "version_a_id": s["version_id"],
        "version_b_id": client.get(f"/mappings/{mapping_id}/trace",
                                   headers=hdr("m", "mapper")).get_json()
        ["target"]["version"]["id"],
    }, headers=hdr("mapper-1", "mapper")).get_json()
    outcomes = {d["from_version_id"]: d["outcome"]
                for d in report["results"][0]["decisions"]}
    assert outcomes[s["version_id"]] == "full"
