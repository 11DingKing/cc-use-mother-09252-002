"""循环映射：A→B→C→A 的等价链必须检测出环且遍历终止。"""
from __future__ import annotations

from tests.conftest import (
    ADMIN, MAPPER, approved_mapping, import_standard, make_unit_payload, unit_id,
)


def _three_country_loop(service, db):
    """搭建三国互认环：CN-A1 ↔ JP-B1 ↔ KR-C1 ↔ CN-A1。"""
    ids = {}
    for code, authority, unit_code in (
        ("STD-CN", "中国人社部", "CN-A1"),
        ("STD-JP", "日本厚劳省", "JP-B1"),
        ("STD-KR", "韩国雇佣部", "KR-C1"),
    ):
        result = import_standard(service, code, authority,
                                 [make_unit_payload(unit_code)])
        ids[unit_code] = unit_id(db, result["version_id"], unit_code)
    pairs = {
        "CN-JP": (ids["CN-A1"], ids["JP-B1"], "中国人社部", "日本厚劳省"),
        "JP-KR": (ids["JP-B1"], ids["KR-C1"], "日本厚劳省", "韩国雇佣部"),
        "KR-CN": (ids["KR-C1"], ids["CN-A1"], "韩国雇佣部", "中国人社部"),
    }
    mappings = {name: approved_mapping(service, *args) for name, args in pairs.items()}
    return ids, mappings


def test_equivalence_chain_detects_cycle_and_terminates(service, db):
    ids, _ = _three_country_loop(service, db)
    chain = service.equivalence_chain(MAPPER, ids["CN-A1"])
    assert set(chain["units"]) == set(ids.values())
    assert chain["has_cycle"] is True
    assert len(chain["cycles"]) == 1
    cycle = chain["cycles"][0]
    # 环首尾相同，途经三个单元
    assert cycle[0] == cycle[-1]
    assert set(cycle[:-1]) == set(ids.values())
    # 三条边都被记录
    assert len(chain["edges"]) == 3


def test_chain_from_any_start_finds_same_cycle(service, db):
    ids, _ = _three_country_loop(service, db)
    for start in ids.values():
        chain = service.equivalence_chain(MAPPER, start)
        assert chain["has_cycle"] is True
        assert set(chain["units"]) == set(ids.values())


def test_revoking_one_edge_breaks_the_cycle(service, db):
    ids, mappings = _three_country_loop(service, db)
    service.revoke(ADMIN, "mapping", mappings["KR-CN"], "机构退出互认")
    chain = service.equivalence_chain(MAPPER, ids["CN-A1"])
    # 链仍然连通（经 JP 中转），但环已消失
    assert set(chain["units"]) == set(ids.values())
    assert chain["has_cycle"] is False
    assert chain["cycles"] == []


def test_proposed_mapping_does_not_extend_chain(service, db):
    ids, _ = _three_country_loop(service, db)
    # 新建一个未会签的映射，不应进入等价链
    result = import_standard(service, "STD-DE", "德国联邦教研部",
                             [make_unit_payload("DE-D1")])
    de_unit = unit_id(db, result["version_id"], "DE-D1")
    service.create_mapping(MAPPER, {
        "source_unit_id": ids["CN-A1"], "target_unit_id": de_unit,
    })
    chain = service.equivalence_chain(MAPPER, ids["CN-A1"])
    assert de_unit not in chain["units"]
