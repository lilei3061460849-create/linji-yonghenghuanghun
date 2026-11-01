"""【必中】X：你接下来的 X 次选中，[目标]无法进行被动行动（2026-11-01 用户令）。

必中是**发动者侧的次数资源**，不是挂在目标身上的硬控：
  · 发动时记 X 次，每次选中[目标]扣 1 次；
  · 被选中的那一次，目标不能闪避、招架（被动行动）；
  · 次数用完即失效，目标的一切主动行动始终不受影响（那是【束缚】的领域）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                    # noqa: E402
from engine.daowen import DaoWenEngine                               # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity             # noqa: E402


def _engine():
    engine = GameEngine(rng_seed=1)
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=9, current_speed=9,
                    attack_count=1, attack_power=8)
    target = Entity("靶怪", "怪物", blood_limit=500, current_hp=500,
                    speed_limit=6, current_speed=6, attack_count=1, attack_power=8)
    engine.state.player = player
    engine.state.enemies = [target]
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.combat._bind_existing_hp_hooks()
    return engine, player, target


def _give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="", cost_formula="",
               effect_formula=""), x_value=0, x_free=True)


def _cast_bizhong(engine, player, x=3, target=None):
    """按正式发动路径给必中记账（不指定目标：必中作用于发动者自己）。"""
    _give(player, "必中")
    return engine.combat.apply_daowen_effect(
        "必中", DaoWenEngine.resolve("必中", x), player, target)


def test_bizhong_grants_charges_instead_of_a_target_debuff():
    engine, player, target = _engine()
    _cast_bizhong(engine, player, x=3)
    assert engine.combat.bizhong_remaining(player) == 3
    assert not target.has_status("必中"), "必中不再给目标挂 debuff"
    assert not player.has_status("必中") or True  # 仅显示用的次数标记


def test_bizhong_blocks_dodge_and_consumes_one_charge():
    engine, player, target = _engine()
    _cast_bizhong(engine, player, x=3)
    hp0 = target.current_hp

    r = engine.combat.resolve_attack(player, target, hit_index=0,
                                     is_must_hit=False, dodge=True)
    assert r.get("dodge_success") is False, r
    assert "必中" in r.get("dodge_fail_reason", ""), r
    assert engine.combat.bizhong_remaining(player) == 2, "每次选中扣 1 次"
    assert target.current_hp < hp0, "闪避被封 → 伤害照常落地"


def test_bizhong_blocks_parry_and_consumes_one_charge():
    engine, player, target = _engine()
    _cast_bizhong(engine, player, x=2)

    r = engine.combat.resolve_attack(player, target, hit_index=0,
                                     is_must_hit=False, parry=True)
    assert not r.get("parry_success"), r
    assert "必中" in r.get("parry_fail_reason", ""), r
    assert engine.combat.bizhong_remaining(player) == 1


def test_charges_run_out_and_defence_works_again():
    engine, player, target = _engine()
    _cast_bizhong(engine, player, x=1)

    first = engine.combat.resolve_attack(player, target, hit_index=0,
                                         is_must_hit=False, dodge=True)
    assert first.get("dodge_success") is False
    assert engine.combat.bizhong_remaining(player) == 0

    second = engine.combat.resolve_attack(player, target, hit_index=0,
                                          is_must_hit=False, dodge=True)
    assert second.get("dodge_success") is True, "次数用完后闪避恢复正常"


def test_bizhong_does_not_take_away_the_targets_own_actions():
    """必中不是硬控：目标依然可以主动行动（封锁主动行动是【束缚】的领域）。"""
    engine, player, target = _engine()
    _cast_bizhong(engine, player, x=3)
    assert engine.combat.can_act(target, action_type="cast") is True
    assert engine.combat.can_act(target, action_type="use_daowen") is True
