"""【束缚】X：受束缚者只能施法（2026-11-01 用户令）。

束缚把可选行动压到只剩「施法」一项。闪避、招架本身就是行动（只是不占出手预算），
所以同样不可选——这里把三件事钉住：闪避被封、招架被封、解除后可照常使用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                    # noqa: E402
from engine.models import Entity, StatusEffect                       # noqa: E402


def _engine():
    engine = GameEngine(rng_seed=1)
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=30, current_mana=30, speed_limit=9, current_speed=9,
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


def _bind(target):
    target.add_status(StatusEffect("束缚", 2, 1, "镇魂铁钉"))


def test_shufu_blocks_dodge():
    """闪避是行动，束缚下不可选：不扣速度，伤害照常落地。"""
    engine, player, target = _engine()
    _bind(target)
    hp0, speed0 = target.current_hp, target.current_speed

    r = engine.combat.resolve_attack(player, target, hit_index=0,
                                     is_must_hit=False, dodge=True)

    assert r.get("dodge_success") is False, r
    assert "束缚" in r.get("dodge_fail_reason", ""), r
    assert target.current_speed == speed0, "被封的闪避不得支付速度"
    assert target.current_hp < hp0, "闪避被封 → 伤害照常落地"


def test_shufu_blocks_parry():
    """招架同样是行动，束缚下不可选：不扣速度，伤害照常落地。"""
    engine, player, target = _engine()
    _bind(target)
    hp0, speed0 = target.current_hp, target.current_speed

    r = engine.combat.resolve_attack(player, target, hit_index=0,
                                     is_must_hit=False, parry=True)

    assert not r.get("parry_success"), r
    assert "束缚" in r.get("parry_fail_reason", ""), r
    assert target.current_speed == speed0, "被封的招架不得支付速度"
    assert target.current_hp < hp0, "招架被封 → 伤害照常落地"


def test_unbound_target_still_dodges_and_parries():
    """同一套结算、只差一个束缚状态：解除后闪避与招架照常成功。"""
    engine, player, target = _engine()
    r = engine.combat.resolve_attack(player, target, hit_index=0,
                                     is_must_hit=False, dodge=True)
    assert r.get("dodge_success") is True, r

    engine, player, target = _engine()
    r = engine.combat.resolve_attack(player, target, hit_index=0,
                                     is_must_hit=False, parry=True)
    assert r.get("parry_success") is True, r
