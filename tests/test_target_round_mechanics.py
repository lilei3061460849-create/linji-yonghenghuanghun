"""通用「角色经历一个回合」机制（Issue #101）。

世界回合数是统一度量衡：`CombatEngine.advance_target_rounds()` 只推进**目标角色**
自己的回合——回始/回增/回终、凡庸计数、有限状态到期与清理——而：

- 不修改 `state.current_round`（世界回合数）；
- 不推进其他角色的回合或状态；
- 不执行全场回合结算（增援计时、封印计时、胜负结算、灾厄轮盘等）；
- 不提供额外行动机会，也不覆盖目标在当前世界回合已有的行动/伤害记录。

【大梦】X 是这条路径的对外入口（目标额外经历X个回合）；【沉溺】X 是其反转，
在目标即将进入下一次回合时消耗1次阻断并跳过该次完整回合。本文件的断言针对
通用路径本身，不针对某一条道纹写特例。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity, Relic, StatusEffect  # noqa: E402
from tests.test_daowen_wiring import _engine, _give, _monster  # noqa: E402


def _world_round_10(engine):
    engine.execute_action("round_start", {})
    engine.state.current_round = 10
    return engine


def test_dameng_three_extra_rounds_keep_world_round_unchanged():
    engine = _engine("target_round_world")
    _give(engine.state.player, "大梦")
    target = _monster(engine, "大梦靶怪", hp=200)
    _world_round_10(engine)
    result = engine.execute_action("use_daowen", {
        "daowen_name": "大梦", "x": 3, "target": target.name,
        "dodge": False, "blood_shadow": False,
    })
    assert result["success"], result
    assert engine.state.current_round == 10
    extra = [e for e in result["execution"]["effects"]
             if isinstance(e, dict) and e.get("type") == "extra_target_round"]
    assert len(extra) == 3
    assert all(e["round"] == 10 for e in extra)


def test_dameng_does_not_grant_actions_or_move_other_characters():
    engine = _engine("target_round_no_action")
    player = engine.state.player
    _give(player, "大梦")
    target = _monster(engine, "大梦靶怪", hp=200)
    other = _monster(engine, "旁观者", hp=200)
    other.add_status(StatusEffect(name="旁观状态", value=1, remaining_rounds=3, source="test"))
    engine.state.monster_reinforcements = [
        {"name": "迟到怪", "arrive_round": 12, "blood_limit": 10, "current_hp": 10,
         "mana_limit": 0, "current_mana": 0, "speed_limit": 1, "current_speed": 1,
         "attack_count": 1, "attack_power": 1, "dao_wen": [], "relics": []},
    ]
    _world_round_10(engine)
    subphase = engine.state.combat_subphase
    turn = engine.state.duel_turn
    player_actions_before = player.actions_used_this_round

    effects = engine.combat.advance_target_rounds(target, 3, source="大梦")

    assert len([e for e in effects if e.get("type") == "extra_target_round"]) == 3
    # 世界时间与全场流程不变
    assert engine.state.current_round == 10
    assert engine.state.combat_subphase == subphase
    assert engine.state.duel_turn == turn
    assert len(engine.state.monster_reinforcements) == 1
    # 其他角色的回合与状态不被推进
    assert other.get_status_effects("旁观状态")[0].remaining_rounds == 3
    assert other.no_action_rounds == 0 and other.no_damage_rounds == 0
    # 额外经历的回合不创造行动机会，也不改写当前世界回合的出手记录
    assert player.actions_used_this_round == player_actions_before
    assert target.actions_used_this_round == 0


def test_dameng_decays_and_expires_only_the_target_statuses():
    engine = _engine("target_round_status")
    target = _monster(engine, "大梦靶怪", hp=200)
    target.add_status(StatusEffect(name="短状态", value=1, remaining_rounds=2, source="test"))
    target.add_status(StatusEffect(name="长状态", value=1, remaining_rounds=9, source="test"))
    _world_round_10(engine)
    engine.combat.advance_target_rounds(target, 2, source="大梦")
    assert not target.has_status("短状态")
    assert target.get_status_effects("长状态")[0].remaining_rounds == 7


def test_extra_rounds_drive_mediocrity_threshold():
    """【凡庸】按角色实际经历的回合推进，额外回合同样计数并触发。"""
    engine = _engine("target_round_mediocrity")
    target = _monster(engine, "大梦靶怪", hp=200)
    target.no_action_rounds = 3
    target.no_damage_rounds = 3
    _world_round_10(engine)

    effects = engine.combat.advance_target_rounds(target, 2, source="大梦")

    # 第 4、5 个额外回合推进到阈值，第 5 个回合触发凡庸并命零；结算随即中断
    assert any(e.get("type") == "mediocrity" for e in effects)
    assert target.current_hp == 0
    assert not target.is_alive
    assert len([e for e in effects if e.get("type") == "extra_target_round"]) == 2


def test_nightwatch_uses_world_round_for_every_extra_round():
    engine = _engine("target_round_lamp")
    target = Entity(
        name="持灯轮回者", entity_type="轮回者",
        blood_limit=100, current_hp=100,
        mana_limit=100, current_mana=0,
        speed_limit=3, current_speed=3,
    )
    engine.state.enemies.append(target)
    engine.state.opponent_relics.append(
        Relic("守夜灯", "每经历一个回合获得当前世界回合数的法力"))
    engine.state.current_round = 10

    effects = engine.combat.advance_target_rounds(target, 3, source="大梦")

    assert engine.state.current_round == 10
    assert target.current_mana == 30
    # 守夜灯走通用回增触发器，每次只读取当前世界回合数（10）作为收益。
    lamp = [e for e in effects if isinstance(e, str) and "守夜灯" in e]
    assert len(lamp) == 3
    assert all("获得10点法力" in line for line in lamp)


def test_chenni_consumes_one_block_per_round_without_advancing_state():
    """【沉溺】阻断的是「经历回合」本身：被跳过的回合不做回合内结算。"""
    engine = _engine("target_round_chenni")
    target = _monster(engine, "沉溺靶怪", hp=200)
    target.add_status(StatusEffect(name="沉溺", value=2, remaining_rounds=-1, source="沉溺"))
    target.add_status(StatusEffect(name="受测状态", value=1, remaining_rounds=3, source="test"))
    target.no_action_rounds = 1
    _world_round_10(engine)

    effects = engine.combat.advance_target_rounds(target, 2, source="大梦")

    assert all(e.get("type") != "extra_target_round" for e in effects)
    blocked = [e for e in effects if e.get("type") == "chenni_round_blocked"]
    assert len(blocked) == 2
    assert not target.has_status("沉溺")
    # 被中断的回合不递减状态、不推进凡庸
    assert target.get_status_effects("受测状态")[0].remaining_rounds == 3
    assert target.no_action_rounds == 1
    assert engine.state.current_round == 10
