"""版本分叉：源标准更新标记受影响映射，分叉分支互不干扰，历史决定不被改写。"""
from __future__ import annotations

from service_09252_002.domain import MappingStatus, VersionStatus
from tests.conftest import (
    ADMIN, APPROVER_A, APPROVER_B, IMPORTER, MAPPER,
    approved_mapping, import_standard, make_unit_payload, unit_id,
)

AUTH_S = "源国技能局"
AUTH_T = "目标国技能院"


def _base(service, db):
    """S.1.0.u1 ↔ T.1.0.v1，已会签并完成一次比对。"""
    s = import_standard(service, "STD-S", AUTH_S, [make_unit_payload("u1")])
    t = import_standard(service, "STD-T", AUTH_T, [make_unit_payload("v1")])
    u1 = unit_id(db, s["version_id"], "u1")
    v1 = unit_id(db, t["version_id"], "v1")
    mapping_id = approved_mapping(service, u1, v1, AUTH_S, AUTH_T)
    report = service.compare_versions(MAPPER, s["version_id"], t["version_id"])
    return s, t, u1, v1, mapping_id, report


def test_source_update_marks_mapping_under_review(service, db):
    s, t, _, _, mapping_id, _ = _base(service, db)
    published = service.publish_version(IMPORTER, "STD-S", {
        "label": "2.0", "units": [make_unit_payload("u2")],
    })
    assert published["affected_mapping_ids"] == [mapping_id]
    mapping = db.get_mapping(mapping_id)
    assert mapping["status"] == MappingStatus.UNDER_REVIEW
    assert mapping["affected_by_update"] is True
    # 旧版本被取代
    assert db.get_version(s["version_id"])["status"] == VersionStatus.SUPERSEDED


def test_history_decisions_are_never_rewritten(service, db):
    s, t, _, _, mapping_id, report = _base(service, db)
    before = service.trace_mapping(MAPPER, mapping_id)["decisions"]
    assert len(before) == 2  # 双向各一条
    service.publish_version(IMPORTER, "STD-S", {
        "label": "2.0", "units": [make_unit_payload("u2")],
    })
    after = service.trace_mapping(MAPPER, mapping_id)["decisions"]
    # 发布新版本只追加标记，不新增、不修改决定
    assert after == before
    assert {d["outcome"] for d in after} == {"full"}
    # 受影响标记出现在审计追溯中
    actions = [e["action"] for e in service.trace_mapping(MAPPER, mapping_id)["events"]]
    assert "marked_affected" in actions


def test_fork_does_not_touch_sibling_branch(service, db):
    s, t, _, v1, mapping_id, _ = _base(service, db)
    # 主分支 2.0
    v2 = service.publish_version(IMPORTER, "STD-S", {
        "label": "2.0", "units": [make_unit_payload("u2")],
    })
    u2 = unit_id(db, v2["version_id"], "u2")
    m2 = approved_mapping(service, u2, v1, AUTH_S, AUTH_T)
    # 从 1.0 分叉出地区分支 2.0-alt：只影响 1.0 的映射，不影响主分支 2.0 的映射
    fork = service.publish_version(IMPORTER, "STD-S", {
        "label": "2.0-alt", "parent_version_label": "1.0",
        "units": [make_unit_payload("u2x")],
    })
    assert mapping_id in fork["affected_mapping_ids"]  # 共同祖先的映射再次受影响
    assert m2 not in fork["affected_mapping_ids"]
    assert db.get_mapping(m2)["status"] == MappingStatus.APPROVED
    assert db.get_mapping(m2)["affected_by_update"] is False
    # 主分支 2.0 未被分叉取代
    assert db.get_version(v2["version_id"])["status"] == VersionStatus.PUBLISHED


def test_reapproval_after_review_clears_affected_flag(service, db):
    s, t, _, _, mapping_id, _ = _base(service, db)
    service.publish_version(IMPORTER, "STD-S", {
        "label": "2.0", "units": [make_unit_payload("u2")],
    })
    assert db.get_mapping(mapping_id)["status"] == MappingStatus.UNDER_REVIEW
    # 新一轮会签（同一轮次内各方已签记录不冲突）
    service.approve(APPROVER_A, "mapping", mapping_id, AUTH_S)
    service.approve(APPROVER_B, "mapping", mapping_id, AUTH_T)
    mapping = db.get_mapping(mapping_id)
    assert mapping["status"] == MappingStatus.APPROVED
    assert mapping["affected_by_update"] is False


def test_new_version_lineage_marks_only_ancestors(service, db):
    s, t, u1, v1, mapping_id, _ = _base(service, db)
    v2 = service.publish_version(IMPORTER, "STD-S", {
        "label": "2.0", "units": [make_unit_payload("u2")],
    })
    u2 = unit_id(db, v2["version_id"], "u2")
    m2 = approved_mapping(service, u2, v1, AUTH_S, AUTH_T)
    # 3.0 继承 2.0：影响 2.0 与 1.0 的映射
    v3 = service.publish_version(IMPORTER, "STD-S", {
        "label": "3.0", "units": [make_unit_payload("u3")],
    })
    assert set(v3["affected_mapping_ids"]) == {mapping_id, m2}
    impact = service.version_impact(MAPPER, v2["version_id"])
    flagged = {m["mapping_id"] for m in impact["mappings"] if m["affected_by_update"]}
    assert m2 in flagged
