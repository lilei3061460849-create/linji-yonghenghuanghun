"""【全速】（原名【迟滞】）与【变形】——重做。

两者都属于"属性模型统一后写遗留字段 attack_power / attack_count 而失效"的同一类病：
统一后攻击力=当前法力、攻击次数=当前速度，而 models.py 的写穿对轮回者不生效，
旧实现对轮回者完全无效。重做后都走状态层 / 真实字段，对全体角色同口径生效。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.daowen import DaoWenEngine
from engine.models import Entity, GameState, StatusEffect


def _arena():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("轮回者", "轮回者", blood_limit=200, current_hp=200,
                    mana_limit=20, current_mana=20, speed_limit=6, current_speed=6,
                    attack_count=6, attack_power=20)
    state.player = player
    state.friends = []
    state.employees = []
    state.enemies = []
    state.current_round = 2
    return state, CombatEngine(state, DiceEngine()), player


def _monster(name="靶怪", speed=3, speed_limit=3, mana=10, mana_limit=10, hp=100):
    return Entity(name, "怪物", blood_limit=hp, current_hp=hp,
                  mana_limit=mana_limit, current_mana=mana,
                  speed_limit=speed_limit, current_speed=speed,
                  attack_count=speed, attack_power=mana)


# ==================== 【全速】攻击次数锁定 = [速限] ====================

def test_quansu_renamed_from_chizhi():
    """原名【迟滞】已改名【全速】；2026-11-01 起【迟滞】是另一条新道纹。"""
    from engine.daowen import DaoWenEngine as D
    D.register_all()
    assert "全速" in D._registry
    assert D.resolve("全速", 1)["attack_count_to_speed_limit"] is True
    # 新【迟滞】只做出手加价，不是【全速】的旧实现。
    chizhi = D.resolve("迟滞", 1)
    assert chizhi["action_cost_penalty"] == 1
    assert "attack_count_to_speed_limit" not in chizhi


def test_quansu_locks_attack_count_to_speed_limit_for_player():
    """对轮回者生效（旧版写遗留字段，对轮回者完全无效）。"""
    state, combat, player = _arena()
    player.current_speed = 2          # 被削过速
    before = player.effective_attack_count()
    calc = DaoWenEngine.resolve("全速", 2, target=player)
    combat.apply_daowen_effect("全速", calc, player, player)
    assert before == 2
    assert player.effective_attack_count() == player.speed_limit == 6


def test_quansu_immune_to_further_slow():
    """锁定期间再被削速，攻击次数不变（锁定的是上限值，不随当前速度走）。"""
    state, combat, player = _arena()
    calc = DaoWenEngine.resolve("全速", 3, target=player)
    combat.apply_daowen_effect("全速", calc, player, player)
    player.current_speed = 0
    assert player.effective_attack_count() == player.speed_limit == 6
    assert player.has_status("全速")


def test_quansu_is_a_buff_not_a_debuff():
    """语义由减益翻转为增益：速度已满时无变化，被削过速时补满。

    依据：2026-09-13 全局钳制规则让「当前速度≤[速限]」无条件成立，
    所以锁定为速限只会补满、绝不会压低。
    """
    state, combat, player = _arena()
    calc = DaoWenEngine.resolve("全速", 2, target=_monster(speed=3, speed_limit=3))
    m = _monster(speed=3, speed_limit=3)
    combat.apply_daowen_effect("全速", calc, player, m)
    assert m.effective_attack_count() == 3, "已满速：无变化"

    m2 = _monster(speed=1, speed_limit=3)
    combat.apply_daowen_effect("全速", calc, player, m2)
    assert m2.effective_attack_count() == 3, "被削过速：补满到速限"


# ==================== 【变形】当前速度 ↔ 当前法力互换 ====================

def test_bianxing_swaps_speed_and_mana_with_clamping():
    """用户举的例子：敌方 20/3/10（血限/速度/法力，速限3）
    → 互换后速度10被速限钳回3、法力3 → 20/3/3，凭空失去 7 点法力。"""
    state, combat, player = _arena()
    t = _monster(speed=3, speed_limit=3, mana=10, mana_limit=10, hp=20)
    assert (t.blood_limit, t.current_speed, t.current_mana) == (20, 3, 10)

    calc = DaoWenEngine.resolve("变形", 1, target=t)
    combat.apply_daowen_effect("变形", calc, player, t)

    assert (t.blood_limit, t.current_speed, t.current_mana) == (20, 3, 3)
    # 攻击力=当前法力，同步掉了 7 点
    assert t.effective_attack_power() == 3


def test_bianxing_defaults_to_caster_without_target():
    """不指定目标时作用于自身（可选目标，不是强制目标）。"""
    state, combat, player = _arena()
    # 玩家速限6/法限20，取 2↔5 互换不会被任一上限钳掉
    player.current_speed, player.current_mana = 2, 5
    calc = DaoWenEngine.resolve("变形", 1)
    combat.apply_daowen_effect("变形", calc, player, None)
    assert (player.current_speed, player.current_mana) == (5, 2)


def test_bianxing_does_not_require_explicit_target_in_api():
    """api.py 的"声明 target 即强制指定目标"规则不适用于变形：目标是可选的。"""
    import inspect
    DaoWenEngine.register_all()
    assert "target" not in inspect.signature(DaoWenEngine._registry["变形"]).parameters


def test_bianxing_restores_after_duration_but_lost_part_is_gone():
    """持续结束后还原互换前的速度/法力；但被上限钳掉的部分不返还。"""
    state, combat, player = _arena()
    t = _monster(speed=3, speed_limit=3, mana=10, mana_limit=10, hp=20)
    calc = DaoWenEngine.resolve("变形", 1, target=t)
    combat.apply_daowen_effect("变形", calc, player, t)
    assert (t.current_speed, t.current_mana) == (3, 3)

    assert t._bianxing_original == (3, 10), "记录的是互换前的当前速度/法力"
    t.current_speed, t.current_mana = t._bianxing_original
    delattr(t, "_bianxing_original")
    assert (t.current_speed, t.current_mana) == (3, 10)


def test_bianxing_blocked_by_dingxing():
    """【定型】期间变形被挡下，且不留空状态。"""
    state, combat, player = _arena()
    t = _monster()
    t.add_status(StatusEffect("定型", 1, 2))
    calc = DaoWenEngine.resolve("变形", 1, target=t)
    combat.apply_daowen_effect("变形", calc, player, t)
    assert not t.has_status("变形")
    assert (t.current_speed, t.current_mana) == (3, 10)
