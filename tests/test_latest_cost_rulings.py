"""2026-10-27 最新裁定：单项净化不凭空增加属性，增殖支付衰老。"""
import pytest
from engine.daowen import DaoWenEngine
from tests.test_daowen_wiring import _engine, _give, _monster


def test_zengzhi_aging_and_infinite_limit():
    e = _engine("latest_zengzhi")
    p = e.state.player
    _give(p, "增殖")
    m = _monster(e, "靶", hp=100)
    before = p.blood_limit
    r = e.execute_action("use_daowen", {"daowen_name": "增殖", "x": 2, "target": m.name})
    assert r["success"], r
    assert p.blood_limit == before - 2
    assert p.reversible_cost_layers["衰老"] == 2
    assert m.has_status("无限血限")
    assert m.to_dict()["blood_limit"] == "∞"
    assert DaoWenEngine.resolve("增殖", 2)["duration"] == 2


@pytest.mark.parametrize("kind,attr", [("衰老", "blood_limit"), ("枯竭", "mana_limit"),
                                        ("萎缩", "speed_limit"), ("疲惫", "current_speed")])
def test_purification_requires_owned_cost_and_cannot_inflate(kind, attr):
    e = _engine("latest_" + kind)
    _give(e.state.player, "净化")
    m = _monster(e, "靶", hp=100)
    m.current_speed = m.speed_limit = 10
    before = getattr(m, attr)
    params = {"daowen_name": "净化", "x": 3, "target": m.name,
              "cost_choices": [{"type": kind}]}
    rejected = e.execute_action("use_daowen", params)
    assert not rejected["success"]
    assert getattr(m, attr) == before
    e.combat._apply_numeric_cost_part(m, kind, 2)
    result = e.execute_action("use_daowen", params)
    assert result["success"], result
    assert getattr(m, attr) == before
    assert m.reversible_cost_layers[kind] == 0


def test_purification_mutation_is_explicit_single_choice():
    e = _engine("latest_mutation")
    _give(e.state.player, "净化")
    m = _monster(e, "靶", hp=100)
    m.mutation_count = 8
    params = {"daowen_name": "净化", "x": 3, "target": m.name}
    assert not e.execute_action("use_daowen", params)["success"]
    params["cost_choices"] = [{"type": "异变"}]
    r = e.execute_action("use_daowen", params)
    assert r["success"], r
    assert m.mutation_count == 5
