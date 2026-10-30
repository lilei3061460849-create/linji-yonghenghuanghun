"""资源支付、杀伐／庇护、共罪／血契、萎缩／唯一与强化的行为契约。"""
import json

import pytest

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.enums import RESOURCE_TYPES, SPECIAL_COST_TYPES, normalize_payment_type
from engine.gamedata import (
    REGION_EXCLUSIVE_DAOWEN, UNIMPLEMENTED_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN, learnability_error,
)
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity("轮回者", "轮回者", blood_limit=80, current_hp=80,
                    mana_limit=100, current_mana=100, speed_limit=3, current_speed=3)
    first = Entity("甲", "怪物", blood_limit=60, current_hp=60,
                   mana_limit=30, current_mana=30, speed_limit=3, current_speed=3)
    second = Entity("乙", "怪物", blood_limit=60, current_hp=60,
                    mana_limit=30, current_mana=30, speed_limit=3, current_speed=3)
    engine.state.player = player
    engine.state.enemies = [first, second]
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.combat._bind_existing_hp_hooks()
    return engine, player, first, second


def _give(entity, name, *, fixed_x=0):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=fixed_x, x_free=not bool(fixed_x))


def _allow(engine):
    # 每个用例中的连续发动是独立行为探针；不放宽生产交替优先权。
    engine.state.duel_turn = "player_side"
    engine.state.player.actions_used_this_round = 0


def _cast(engine, name, x, *, target_ref="enemy:0", **extra):
    _allow(engine)
    params = {"daowen_name": name, "x": x, "target_ref": target_ref,
              "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
    params.update(extra)
    return engine.execute_action("use_daowen", params)


def _pact(holder, target, multiplier=1, rounds=3):
    holder.add_status(StatusEffect(
        "血契", value=multiplier, remaining_rounds=rounds,
        source=str(target.runtime_id), effect_data={"target_id": str(target.runtime_id)}))


def _share(first, second, rounds=3):
    ids = [str(first.runtime_id), str(second.runtime_id)]
    encoded = json.dumps(ids, ensure_ascii=False)
    for entity in (first, second):
        entity.add_status(StatusEffect(
            "共罪", value=1, remaining_rounds=rounds, source=encoded,
            effect_data={"group_ids": ids}))


def test_resource_taxonomy_and_legacy_aliases():
    assert RESOURCE_TYPES == {"生命", "血限", "法力", "法限", "速度", "速限", "碎片", "假碎片"}
    assert SPECIAL_COST_TYPES == {"枯竭", "失忆", "异变", "冷却"}
    assert normalize_payment_type("流血") == "生命"
    assert normalize_payment_type("衰老") == "血限"
    assert normalize_payment_type("疲惫") == "速度"
    assert normalize_payment_type("萎缩") == "法限"
    assert "出手" not in RESOURCE_TYPES


def test_shaifa_is_direct_life_loss_not_attack_damage():
    engine, player, target, _ = _engine()
    _give(player, "杀伐")
    target.shield = 20
    result = _cast(engine, "杀伐", 3)
    assert result["success"] is True, result
    assert target.current_hp == 51
    assert target.shield == 20
    assert player.current_mana == 97
    effect = next(e for e in result["execution"]["effects"] if e["type"] == "target_life_loss")
    assert effect["requested"] == effect["lost"] == 9
    assert "damage_type" not in result["calculation"]


def test_shelter_blocks_external_losses_by_count_then_expires_by_charges():
    engine, player, target, _ = _engine()
    _give(player, "庇护")
    _give(player, "杀伐")
    target.shield = 20
    assert _cast(engine, "庇护", 2)["success"] is True
    assert target.shield == 20
    assert _cast(engine, "杀伐", 2)["success"] is True
    assert target.current_hp == 60 and target.get_status_value("庇护") == 1
    assert _cast(engine, "杀伐", 2)["success"] is True
    assert target.current_hp == 60 and not target.has_status("庇护")
    assert _cast(engine, "杀伐", 2)["success"] is True
    assert target.current_hp == 56


def test_shelter_does_not_block_own_resource_payment():
    engine, player, external, _ = _engine()
    _give(player, "庇护")
    assert _cast(engine, "庇护", 1, target_ref="player:0")["success"] is True
    payment = engine.combat.pay_resource(
        player, "生命", 3,
        ctx={"actor": player, "target": player, "source": "自付",
             "tags": {"active_payment"}})
    assert payment["actual_paid"] == 3
    assert player.current_hp == 77 and player.has_status("庇护")
    loss = engine.combat._raw_hp_loss(
        player, 4, ctx={"actor": external, "target": player, "source": "外部"})
    assert loss["lost"] == 0
    assert player.current_hp == 77 and not player.has_status("庇护")


def test_blood_pact_uses_nominal_mana_before_discount():
    engine, player, target, _ = _engine()
    _give(player, "血契")
    _give(player, "杀伐")
    assert _cast(engine, "血契", 2)["success"] is True
    player.add_status(StatusEffect("愤怒", remaining_rounds=2))
    assert _cast(engine, "杀伐", 3)["success"] is True
    # 血契支付2；杀伐名义3、愤怒实付2；目标额外支付2×3法力。
    assert player.current_mana == 96
    assert target.current_mana == 24
    assert target.current_hp == 51


def test_blood_pact_forced_shortfall_clamps_to_zero_and_life_can_kill():
    engine, _, payer, target = _engine()
    _pact(payer, target, multiplier=3)
    target.current_mana = 2
    engine.combat.pay_resource(payer, "法力", 1, ctx={"actor": payer, "source": "测试"})
    assert target.current_mana == 0

    target.current_hp = 2
    engine.combat.pay_resource(payer, "生命", 1, ctx={"actor": payer, "source": "测试"})
    assert target.current_hp == 0
    assert not target.is_alive


def test_gongzui_shares_resources_in_both_directions_but_not_special_mechanisms():
    engine, player, first, second = _engine()
    _give(player, "共罪")
    result = _cast(engine, "共罪", 2, dodge_targets=[
        {"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
        {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False},
    ])
    assert result["success"] is True, result
    assert player.current_mana == 98
    assert player.get_status_value("共罪") == 1
    assert first.get_status_value("共罪") == second.get_status_value("共罪") == 1

    before = (player.current_mana, first.current_mana, second.current_mana)
    engine.combat.pay_resource(first, "法力", 5, ctx={"actor": first, "source": "测试"})
    losses = [before[0] - player.current_mana, before[1] - first.current_mana,
              before[2] - second.current_mana]
    assert sum(losses) == 5 and all(loss > 0 for loss in losses)
    before = (player.current_mana, first.current_mana, second.current_mana)
    engine.combat.pay_resource(second, "法力", 4, ctx={"actor": second, "source": "测试"})
    losses = [before[0] - player.current_mana, before[1] - first.current_mana,
              before[2] - second.current_mana]
    assert sum(losses) == 4 and all(loss > 0 for loss in losses)

    engine.combat.pay_numeric_cost(first, "枯竭", 4, ctx={"actor": first, "source": "测试"})
    assert first.pending_exhaust_mana == 4
    assert player.pending_exhaust_mana == second.pending_exhaust_mana == 0


def test_gongzui_blood_pact_cross_is_observed_without_recursive_exemption():
    engine, _, first, second = _engine()
    _share(first, second)
    _pact(first, second)
    engine.combat.pay_resource(first, "生命", 5, ctx={"actor": first, "source": "测试"})
    total_lost = 120 - first.current_hp - second.current_hp
    # 首次5与血契额外5之后，额外支付的共罪份额仍会继续触发A的血契。
    # 余数随机只改变后续1点链的长度，不允许把总额预先写成10。
    assert total_lost >= 13
    assert first.current_hp < 60 and second.current_hp < 60


def test_weisuo_blocks_every_current_speed_gain_but_not_loss():
    engine, player, target, _ = _engine()
    _give(player, "萎缩")
    target.current_speed = 1
    assert _cast(engine, "萎缩", 2)["success"] is True
    assert player.current_mana == 96
    assert engine.combat._gain_speed(target, 3) == 0
    target.current_speed += 2
    assert target.current_speed == 1
    assert engine.combat._lose_current_speed(target, 1) == 1
    assert target.current_speed == 0
    assert engine.combat._gain_speed(target, 1) == 0


def test_unique_history_covers_instant_spell_and_normal_activation():
    engine, player, target, _ = _engine()
    _give(player, "杀伐")
    _give(player, "唯一")
    execution = engine.combat.build_instant_execution(
        player, "发动杀伐X于目标", target, [{"x": 1, "dodge": False}],
        engine.combat._combat_entity_refs())
    step = execution.run_all()[0]
    assert step.status.value == "completed"
    assert player.has_used_daowen("杀伐")

    assert _cast(engine, "唯一", 1, target_ref="player:0")["success"] is True
    mana = player.current_mana
    result = _cast(engine, "杀伐", 1)
    assert result["success"] is False
    assert "已经用过【杀伐】" in result["error"]
    assert player.current_mana == mana
    # 未用过的道纹不被唯一误伤。
    assert DaoWenEngine.resolve("再生", 1, target=player, caster=player)["dao_wen"] == "再生"


def test_qianghua_grants_extra_attack_without_original_attack_and_ruohua_reverses_it():
    engine, player, target, _ = _engine()
    _give(player, "强化")
    _give(player, "弱化")
    target.current_mana = 0
    target.attack_power = 0
    result = _cast(engine, "强化", 2)
    assert result["success"] is True, result
    assert player.current_mana == 98 and player.current_hp == 76
    assert target.current_mana == 0
    assert target.effective_attack_power() == 4
    status = next(s for s in target.status_effects if s.name == "强化")
    assert status.value == 4 and status.remaining_rounds == -1
    assert "额外攻击力" in result["calculation"]["summary"]

    assert _cast(engine, "弱化", 1)["success"] is True
    assert target.effective_attack_power() == 3


def test_learning_pools_and_resonance_edges_match_new_nodes():
    known = set(DaoWenEngine.list_all())
    assert "萎缩" in REGION_EXCLUSIVE_DAOWEN["扭曲都市"]
    assert "唯一" in REGION_EXCLUSIVE_DAOWEN["荒疫古城"]
    assert "血契" not in UNIMPLEMENTED_DAOWEN
    assert UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["血契"] == "永夜庭"
    assert "永夜庭" in learnability_error("血契", owned=set(), region="扭曲都市", known=known)
    assert "尚未接入运行时" not in learnability_error(
        "血契", owned=set(), region="扭曲都市", known=known)

    assert any(edge == ("超频", "反转", "萎缩")
               for edges in ResonanceEngine.CLOSED_LOOPS.values() for edge in edges)
    assert any(edge == ("重演", "反转", "唯一")
               for edges in ResonanceEngine.CLOSED_LOOPS.values() for edge in edges)
    assert any(edge == ("弱化", "反转", "强化")
               for edges in ResonanceEngine.CLOSED_LOOPS.values() for edge in edges)
    assert not any("尘封" in edge for edges in ResonanceEngine.CLOSED_LOOPS.values() for edge in edges)


def test_upper_limit_payment_clamps_current_value_and_save_defaults():
    engine, player, _, _ = _engine()
    engine.combat.pay_resource(player, "法限", 3, ctx={"actor": player, "source": "测试"})
    assert player.mana_limit == 97 and player.current_mana == 97
    legacy = Entity("旧档", "轮回者")
    legacy.__setstate__({"name": "旧档", "entity_type": "轮回者"})
    assert legacy.daowen_use_history == set()
    # 旧唯一代价字段即使存在，也不再充当发动门禁。
    instance = DaoWenInstance(DaoWen("杀伐", "", "", "", ""), spent_unique=True)
    assert instance.can_use() is True
