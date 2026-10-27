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
    assert result["calculation"]["cost"] == 60
    assert e.state.player.current_mana == 20


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


def test_amnesia_is_rejected_without_returning_rune_or_spending_mana():
    e = _engine("refund_amnesia")
    p = e.state.player
    _give(p, "净化")
    _give(p, "固执")
    inst = p.dao_wen["固执"]
    inst.cooldown_remaining = 7
    inst.spent_unique = True
    p.forget_for_cost("固执")
    mana_before = p.current_mana
    actions_before = p.actions_used_this_round
    result = e.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 1, "target_ref": "player:0",
        "cost_choices": [{"type": "失忆"}],
    })
    assert not result["success"], result
    assert "固执" not in p.dao_wen
    assert inst.cooldown_remaining == 7 and inst.spent_unique
    assert p.refundable_daowen() == [inst]
    assert p.current_mana == mana_before
    assert p.actions_used_this_round == actions_before


def test_cooldown_is_rejected_without_altering_either_cooldown():
    e = _engine("refund_cooldown")
    p = e.state.player
    _give(p, "净化")
    _give(p, "固执")
    _give(p, "束缚")
    p.dao_wen["固执"].cooldown_remaining = 2
    p.dao_wen["束缚"].cooldown_rounds_remaining = 6
    result = e.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 3, "target_ref": "player:0",
        "cost_choices": [{"type": "冷却", "daowen_name": "固执"}],
    })
    assert not result["success"], result
    assert p.current_mana == 80
    assert p.actions_used_this_round == 0
    assert p.dao_wen["固执"].cooldown_remaining == 2
    assert p.dao_wen["束缚"].cooldown_rounds_remaining == 6


def test_refund_history_survives_full_entity_snapshot():
    e = _engine("refund_snapshot")
    p = e.state.player
    _give(p, "固执")
    p.dao_wen["固执"].cooldown_remaining = 3
    p.forget_for_cost("固执")
    p.reversible_cost_layers["衰老"] = 2
    restored = e._deserialize_entity_full(e._serialize_entity_full(p))
    assert restored.reversible_cost_layers == {"衰老": 2}
    assert restored.refundable_daowen()[0].dao_wen.name == "固执"
    assert restored.refundable_daowen()[0].cooldown_remaining == 3


def test_dianjin_ally_purification_amnesia_loop_is_blocked():
    """最新范围限制切断旧点金+队友净化循环，不靠提高汇率掩盖漏洞。"""
    from engine.models import Entity, StatusEffect
    e = _engine("dianjin_refund_cycle")
    p = e.state.player
    p.dao_wen.clear()
    for name in ("杀伐", "点金"):
        _give(p, name)
    ally = Entity("净化者", "朋友", blood_limit=120, current_hp=120,
                  mana_limit=80, current_mana=80, speed_limit=4, current_speed=4)
    ally.add_status(StatusEffect("疯狂", -1, value=6))
    _give(ally, "净化")
    e.state.friends = [ally]
    mana_before = ally.current_mana
    shards_before = e.state.shards
    gain = e.execute_action("use_daowen", {"daowen_name": "点金", "x": 1})
    assert gain["success"], gain
    assert "杀伐" not in p.dao_wen
    refund = e.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 1, "actor_ref": "friend:0", "target_ref": "player:0",
        "cost_choices": [{"type": "失忆"}],
    })
    assert not refund["success"], refund
    assert "杀伐" not in p.dao_wen
    assert e.state.shards == shards_before + 80
    assert ally.current_mana == mana_before
    assert ally.actions_used_this_round == 0


@pytest.mark.parametrize("kind", ["流血", "失忆", "冷却", "唯一", "法力"])
def test_non_numeric_cost_types_are_rejected_by_calculator(kind):
    with pytest.raises(ValueError, match="仅支持数值类代价"):
        DaoWenEngine.resolve("净化", 1, cost_choices=[{"type": kind}])


def test_purification_cost_is_twenty_mana_per_layer():
    e = _engine("purification_20x")
    p = e.state.player
    _give(p, "净化")
    m = _monster(e, "靶", hp=100)
    m.mutation_count = 5
    params = {"daowen_name": "净化", "x": 3, "target": m.name,
              "cost_choices": [{"type": "异变"}]}
    p.current_mana = 59
    rejected = e.execute_action("use_daowen", params)
    assert not rejected["success"], rejected
    assert p.current_mana == 59
    assert m.mutation_count == 5
    assert p.actions_used_this_round == 0
    p.current_mana = 60
    accepted = e.execute_action("use_daowen", params)
    assert accepted["success"], accepted
    assert accepted["calculation"]["cost"] == 60
    assert p.current_mana == 0
    assert m.mutation_count == 2
    assert p.actions_used_this_round == 1


def test_stale_amnesia_calculation_cannot_bypass_scope_restriction():
    e = _engine("stale_amnesia_calc")
    with pytest.raises(ValueError, match="仅允许减少一种数值类代价"):
        e.combat.apply_daowen_effect("净化", {
            "x": 1, "cost": 2, "cost_reductions": {"失忆": 1},
        }, e.state.player, e.state.player)
