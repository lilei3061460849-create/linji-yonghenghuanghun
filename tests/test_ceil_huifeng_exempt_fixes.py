"""2026-11-02 第五轮修复回归锁：向上取整 / 【回锋刀】新版 / 豁免集合。

三条规则口径（用户 2026-11-02 指令）：

1. **全系统除法一律向上取整**：`ceil(M/N)=(M+N-1)//N`。共罪组内每个角色只占
   1 份名额（去重），每份都扣 `ceil(M/N)`，**严禁**随机分配余数——2 人分担
   1 点生命时两人各扣 1 点，而不是一人 1、一人 0。
2. **【回锋刀】**：当前速度发生**实际变化**（增加或减少）即触发，每次固定
   **5 点**伤害；缺 `dodge_relic_target_ref` 时自动取缺省目标，绝不抛错。
3. **反向豁免道纹**（【凋零】【压迫】）与【波及】的多目标：所有被选中的
   `[目标]` 属于**同一个豁免集合**，绝不能分次循环导致 A 杀 B、B 杀 A。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic, StatusEffect


def _state() -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                    speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _bind_gongzui(payer: Entity, members: list[Entity], *, duplicates: bool = False) -> None:
    """把共罪状态直接挂到组内每人身上（绕开发动流程，只测分摊数学）。"""
    ids = [str(entity.runtime_id) for entity in members]
    encoded = ",".join(ids)
    for entity in members:
        entity.add_status(StatusEffect(
            name="共罪", value=1, remaining_rounds=-1, source=encoded,
            effect_data={"group_ids": ids + (ids[:1] if duplicates else [])}))


# ==================== 1. 向上取整 ====================

def test_divide_flat_uses_ceil_and_never_randomises_remainder():
    _, combat, _, _ = _state()
    assert combat._divide_flat(1, 2) == [1, 1]          # 1/2 → 每人1，不是[1,0]
    assert combat._divide_flat(5, 3) == [2, 2, 2]
    assert combat._divide_flat(7, 2) == [4, 4]
    assert combat._divide_flat(0, 3) == [0, 0, 0]
    assert combat._divide_flat(-5, 2) == [-3, -3]        # 负数按绝对值取整后还原
    # 反复调用结果必须完全一致：任何随机余数都已废除。
    assert {tuple(combat._divide_flat(5, 3)) for _ in range(20)} == {(2, 2, 2)}


def test_gongzui_two_members_each_pay_ceil_of_one_life():
    """2 人共罪、其中 1 人消耗 1 点生命 → 两人各扣 1 点。"""
    state, combat, player, ally = _state()
    ally.entity_type = "朋友"
    state.friends = [ally]
    _bind_gongzui(player, [player, ally])
    before = (player.current_hp, ally.current_hp)

    combat.pay_numeric_cost(player, "生命", 1)

    assert before[0] - player.current_hp == 1
    assert before[1] - ally.current_hp == 1


def test_gongzui_group_ids_are_deduped_to_one_share_per_character():
    """`group_ids` 里同一 runtime_id 重复出现也只能占 1 份名额。"""
    state, combat, player, ally = _state()
    ally.entity_type = "朋友"
    state.friends = [ally]
    _bind_gongzui(player, [player, ally], duplicates=True)
    assert len(combat._blood_pact_group(player)) == 2

    before = (player.current_hp, ally.current_hp)
    combat.pay_numeric_cost(player, "生命", 4)
    # 4/2=2，每人才扣 2；若错当 3 份额则会是 ceil(4/3)=2 却重复扣玩家。
    assert before[0] - player.current_hp == 2
    assert before[1] - ally.current_hp == 2


def test_divide_quota_conserves_total_for_catalysis_steps():
    _, combat, _, _ = _state()
    assert combat._divide_quota(3, 2) == [2, 1]
    assert combat._divide_quota(5, 3) == [2, 2, 1]
    for total in range(0, 10):
        for count in range(1, 5):
            parts = combat._divide_quota(total, count)
            assert sum(parts) == total
            assert max(parts) <= (total + count - 1) // count


# ==================== 2. 【回锋刀】 ====================

def _with_huifeng(state: GameState) -> None:
    state.relics = [Relic("回锋刀", "")]


def test_huifeng_triggers_on_speed_gain_with_fixed_five_damage():
    state, combat, player, enemy = _state()
    _with_huifeng(state)
    combat._remember_huifeng_target(player, "enemy:0")
    player.current_speed = 5          # 留出加速空间，否则上限把变化吃掉
    hp = enemy.current_hp
    gained = combat._gain_speed(player, 3)
    assert gained == 3
    assert enemy.current_hp == hp - 5          # 固定5点，不再是3×点数


def test_huifeng_triggers_on_speed_loss_with_fixed_five_damage():
    state, combat, player, enemy = _state()
    _with_huifeng(state)
    combat._remember_huifeng_target(player, "enemy:0")
    hp = enemy.current_hp
    combat._lose_current_speed(player, 4)
    assert player.current_speed == 6
    assert enemy.current_hp == hp - 5


def test_huifeng_autopicks_default_target_without_explicit_ref_1v1():
    """1v1 且调用方没有提交目标引用：自动打唯一的敌对存活单位，不抛错。"""
    state, combat, player, enemy = _state()
    _with_huifeng(state)
    hp = enemy.current_hp
    combat._lose_current_speed(player, 1)      # 无 relic_target_ref
    assert enemy.current_hp == hp - 5


def test_huifeng_defaults_to_current_spell_target_when_several_enemies():
    """多敌人时优先取"当前法术步骤的主目标"，而不是随便挑一个。"""
    state, combat, player, first = _state()
    second = Entity("M2", "怪物", blood_limit=100, current_hp=100)
    state.enemies = [first, second]
    _with_huifeng(state)
    hp_first, hp_second = first.current_hp, second.current_hp
    combat._huifeng_spell_target = second
    try:
        combat._lose_current_speed(player, 1)
    finally:
        combat._huifeng_spell_target = None
    assert second.current_hp == hp_second - 5
    assert first.current_hp == hp_first


def test_huifeng_does_not_fire_when_speed_is_capped_by_limit():
    """速限封顶导致当前速度没有实际变化 → 不触发。"""
    state, combat, player, enemy = _state()
    _with_huifeng(state)
    player.current_speed = player.speed_limit
    hp = enemy.current_hp
    assert combat._gain_speed(player, 2) == 0
    assert enemy.current_hp == hp


# ==================== 3. 豁免集合 ====================

def _diaoling(combat: CombatEngine, caster: Entity, target: Entity, x: int) -> dict:
    calc = DaoWenEngine.resolve("凋零", x, target=target, caster=caster)
    return combat.apply_daowen_effect("凋零", calc, caster, target)


def test_diaoling_wave_targets_form_one_exempt_set():
    """波及把 A、B 都纳入[目标] → 两人都不该死，只有集合外的 C 死。"""
    state, combat, caster, a = _state()
    b = Entity("B", "怪物", blood_limit=10, current_hp=2)
    c = Entity("C", "怪物", blood_limit=10, current_hp=2)
    state.enemies = [a, b, c]
    a.current_hp = b.current_hp = c.current_hp = 2
    for entity in (a, b):
        entity.add_status(StatusEffect(name="波及", value=1, remaining_rounds=-1,
                                       source=caster.name))

    result = _diaoling(combat, caster, a, 3)

    assert a.is_alive and b.is_alive, result
    assert not c.is_alive


def test_diaoling_exempts_caster_when_target_is_self():
    state, combat, caster, a = _state()
    state.enemies = [a]
    caster.current_hp = 2
    a.current_hp = 50
    _diaoling(combat, caster, caster, 3)
    assert caster.is_alive


def test_yapo_exempt_set_lets_both_targets_act_freely():
    """A、B 同属豁免集合 → 双方都能正常选目标；集合外的 D 仍被锁成只能选自己。"""
    state, combat, caster, a = _state()
    b = Entity("B", "怪物", blood_limit=50, current_hp=50)
    d = Entity("D", "怪物", blood_limit=50, current_hp=50)
    state.enemies = [a, b, d]
    for entity in (a, b):
        entity.add_status(StatusEffect(name="压迫", value=1, remaining_rounds=2,
                                       source=caster.name))

    assert combat.is_targetable(a, d), "A 是豁免目标，应能选 D"
    assert combat.is_targetable(b, d), "B 是豁免目标，应能选 D"
    assert not combat.is_targetable(d, a), "D 不在豁免集合，只能选自己"
    assert combat.is_targetable(d, d)


# ==================== 4. 文档口径 ====================

def test_rule_texts_state_ceil_and_new_interrupt_wording():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rules = open(os.path.join(root, "规则正文.md"), encoding="utf-8").read()
    assert "余数按随机数分配" not in rules
    assert "向上取整" in rules
    spells = open(os.path.join(root, "法术索引.md"), encoding="utf-8").read()
    assert "某一步未能实际生效" not in spells
    assert "某一步未能成功发动" in spells
    items = open(os.path.join(root, "物品索引.md"), encoding="utf-8").read()
    assert "对[目标]造成5点伤害" in items
    assert "对[目标]造成3点伤害" not in items
