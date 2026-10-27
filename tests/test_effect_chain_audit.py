"""因果链收尾审计回归测试（2026-08-19）。

本文件不测“最后 HP 是多少”，而是钉死架构约束：
  A 单层效果    —— 伤害产生 EffectContext + CombatEvent，source/target 正确
  B 两层链      —— 伤害 → 千荆甲反噬，parent_event_id 正确
  C 三层链      —— 道纹 → 血限变化 → 命零，parent_event_id 连续
  D 防双触发    —— 旧逻辑 + Hook 不会让同一效果跑两次
  E 死亡流程    —— HP=0 → 统一死亡判定 → ENTITY_DIED → 死后效果
  F 顺序显式化  —— Hook 按 priority 执行，且顺序与重构前一致
  G 无 0HP 存活体 —— 血限压迫致死必须走统一命零判定
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.combat_hooks import (
    CombatHookManager,
    QianjingjiaHook,
    AfterDamageEffectsHook,
)
from engine.dice import DiceEngine
from engine.enums import EffectPolarity
from engine.mechanisms import MECHANISMS, MechanismHookAdapter
from engine.models import Entity, GameState, Relic, StatusEffect


def _arena(player_hp: int = 100, enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=player_hp,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


# ==================== A. 单层效果 ====================

def test_a_single_layer_damage_has_context_and_event():
    """一次伤害：EffectContext 有来源，CombatEvent 记录事实，两者职责分离。"""
    state, combat, player, enemy = _arena()

    detail = combat._apply_hostile_damage(enemy, 12, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 12, "tags": {"daowen"}, "event_id": "A-1",
    })

    # EffectContext：为什么发生 / 来自哪里
    assert detail["ctx"]["source"] == "杀伐"
    assert detail["ctx"]["source_type"] == "daowen"
    assert detail["ctx"]["actor"] == "P" and detail["ctx"]["target"] == "M"
    assert detail["ctx"]["mechanic"] == "damage"
    assert "context_warning" not in detail, "正常战斗路径不得退化到 legacy fallback"

    # hp_loss 是伤害的子事件
    assert detail["hp_loss_ctx"]["parent_event_id"] == "A-1"

    # CombatEvent：发生了什么
    applied = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(applied) == 1
    evt = applied[0]
    assert evt.actor_name == "P" and evt.target_name == "M"
    assert evt.data["actual_damage"] == 12
    assert evt.event_id == "A-1"
    # 事件流事实源挂在 state 上，combat.event_stream 只是别名视图
    assert combat.event_stream is state.combat_events


def test_a_legacy_call_without_ctx_is_marked_not_silent():
    """兼容层仍在，但必须显式打标，禁止静默漏源。"""
    _, combat, player, enemy = _arena()
    detail = combat._apply_hostile_damage(enemy, 5, source=player)
    assert detail["ctx"]["source_type"] == "legacy"
    assert "context_warning" in detail


# ==================== B. 两层链：攻击伤害 → 千荆甲反噬 ====================
# 2026-10-03：【爆裂】道纹删除；反噬语义由通用遗物【千荆甲】承接
# （你受到攻击伤害前，攻击者受到等量伤害）。

def _qianjingjia_arena(player_hp: int = 100, enemy_hp: int = 100):
    state, combat, player, enemy = _arena(player_hp=player_hp, enemy_hp=enemy_hp)
    state.relics.append(Relic(name="千荆甲", effect=""))
    return state, combat, player, enemy


def test_b_two_layer_chain_reflect_parent_is_damage():
    """千荆甲反噬的失血必须挂在这次攻击伤害之下。"""
    state, combat, player, enemy = _qianjingjia_arena(player_hp=100, enemy_hp=100)

    detail = combat._apply_hostile_damage(player, 10, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"}, "event_id": "B-1",
    })

    assert enemy.current_hp == 90, "反噬数值口径：等量（10 点）"
    assert detail["actual_damage"] == 10
    reflect = [e for e in enemy._hp_loss_events if e["subtype"] == "relic_reflect"]
    assert len(reflect) == 1, "反噬只能记一次"
    assert reflect[0]["parent_event_id"] == detail["ctx"]["event_id"], \
        "反噬失血必须挂在这一笔伤害的 event_id 之下"
    assert reflect[0]["amount"] == 10
    assert reflect[0]["source"] == "千荆甲"


def test_b_lethal_reflect_death_is_traceable():
    """反噬致死：死亡上下文必须能追回这次伤害，不能是 legacy_death。"""
    state, combat, player, enemy = _qianjingjia_arena(player_hp=100, enemy_hp=6)

    combat._apply_hostile_damage(player, 10, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"}, "event_id": "B-2",
    })

    assert enemy.is_alive is False
    assert enemy._death_ctx["mechanic"] == "death"
    assert enemy._death_ctx["source_type"] != "legacy"
    assert enemy._death_ctx["parent_event_id"] is not None
    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1


# ==================== C. 三层链：道纹 → 血限变化 → 命零 ====================

def test_c_three_layer_chain_daowen_blood_limit_death():
    """瓦解100%：道纹结算 → 血限归零 → 生命归零 → 统一命零，父链连续。"""
    _, combat, player, enemy = _arena(enemy_hp=20, enemy_bl=20)

    result = combat.apply_daowen_effect("瓦解", {"x": 10, "blood_limit_pct": 100}, player, enemy)

    root_id = result["daowen_ctx"]["event_id"]
    bl_events = enemy._blood_limit_events
    assert len(bl_events) == 1
    assert bl_events[0]["mechanic"] == "blood_limit_change"
    assert bl_events[0]["source"] == "瓦解"
    assert bl_events[0]["parent_event_id"] == root_id, "第二层必须指向道纹结算"

    assert enemy.blood_limit == 0 and enemy.current_hp == 0
    assert enemy.is_alive is False, "血限压到 0 生命却仍存活 = 非法状态"
    assert enemy._death_ctx["parent_event_id"] == bl_events[0]["event_id"], \
        "第三层必须指向血限变化，而不是直接跳回道纹"

    died = _events(combat, CombatEventType.ENTITY_DIED)
    changed = _events(combat, CombatEventType.BLOOD_LIMIT_CHANGED)
    assert len(died) == 1 and len(changed) == 1


def test_c_cut_blood_limit_change_parents_the_damage():
    """切割道纹已删除（2026-08-21）：残留切割状态不再产生血限扣除；伤痕同构 ctx 链见下条。"""
    _, combat, player, enemy = _arena(enemy_hp=100)
    player.add_status(StatusEffect("切割", value=1, remaining_rounds=3, source="test"))

    detail = combat._apply_hostile_damage(enemy, 7, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 7, "tags": {"daowen"}, "event_id": "C-2",
    })

    assert enemy.blood_limit == 100, "切割已删除，失血不再扣除等量血限"
    assert "qiege_ctx" not in detail and "qiege_blood_loss" not in detail


def test_c_scar_blood_limit_change_now_has_context_too():
    """伤痕此前完全没有上下文，只有切割有；两者现在同构。"""
    _, combat, player, enemy = _arena(enemy_hp=100)
    enemy.add_status(StatusEffect("伤痕", value=4, remaining_rounds=-1, source="P"))

    detail = combat._apply_hostile_damage(enemy, 6, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 6, "tags": {"daowen"}, "event_id": "C-3",
    })

    assert enemy.blood_limit == 96, "伤痕数值口径不得改变"
    assert detail["shanghen_ctx"]["mechanic"] == "blood_limit_change"
    assert detail["shanghen_ctx"]["subtype"] == "scar"
    assert detail["shanghen_ctx"]["parent_event_id"] == "C-3"


# ==================== D. 防双触发 ====================

def test_d_after_damage_effects_run_exactly_once():
    """一次伤害 = 一次落地后结算。伤痕只扣一次血限，不是两次。"""
    _, combat, player, enemy = _arena(enemy_hp=100)
    enemy.add_status(StatusEffect("伤痕", value=9, remaining_rounds=-1, source="test"))

    combat._apply_hostile_damage(enemy, 9, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 9, "tags": {"daowen"},
    })

    assert enemy.blood_limit == 91
    assert len(enemy._blood_limit_events) == 1
    assert len(_events(combat, CombatEventType.BLOOD_LIMIT_CHANGED)) == 1
    assert len(_events(combat, CombatEventType.DAMAGE_APPLIED)) == 1


def test_d_generic_after_damage_dispatch_skips_pipeline_hook():
    """通用遍历不得重跑显式分发的 Hook —— 否则伤痕/切割/寄生会各触发两次。"""
    manager = CombatHookManager()
    assert any(isinstance(h, AfterDamageEffectsHook) for h in manager.hooks())
    # 传 None 作为 combat：一旦 AfterDamageEffectsHook 被误跑，必然 AttributeError。
    victim = Entity("V", "怪物", blood_limit=50, current_hp=50)
    victim.add_status(StatusEffect("伤痕", value=3, remaining_rounds=-1, source="x"))
    res = manager.apply_after_damage(victim, 5, 0, {"ctx": None}, None, None)
    assert res == {}
    assert victim.blood_limit == 50, "通用遍历不应该动血限"


def test_d_hook_manager_reuses_single_instances():
    """显式分发口与注册表必须指向同一个实例，不能各 new 一份。"""
    manager = CombatHookManager()
    hooks = manager.hooks()
    assert manager.redirection_hook in hooks
    assert manager.mitigation_hook in hooks
    assert manager.after_damage_hook in hooks
    assert len([h for h in hooks if isinstance(h, AfterDamageEffectsHook)]) == 1


def test_d_death_notification_is_idempotent():
    """同一实体多次死亡通知只产生一次 ENTITY_DIED 与一次死后效果。"""
    _, combat, player, enemy = _arena(enemy_hp=8)
    combat.state.relics = [Relic("焦黑发丝", "")]
    player.current_speed = 5

    combat._apply_hostile_damage(enemy, 20, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"},
    })
    speed_after_first = player.current_speed

    # 再从别的入口重复宣告死亡
    combat._on_entity_death(enemy)
    combat._check_hp_zero_death(enemy)

    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1
    assert player.current_speed == speed_after_first == 7, "焦黑发丝只能触发一次"


def test_d_dodge_grants_shield_once_through_engine_path():
    """避风铃有 legacy(_note_dodge) 与 Hook 两套实现；引擎路径只能加一次 3 点格挡。"""
    state, combat, player, _ = _arena()
    state.relics = [Relic("避风铃", "")]
    player.shield = 0
    player.current_speed = 5

    combat._spend_dodge_speed(player)

    assert player.shield == 3, "旧逻辑 + Hook 双触发会变成 6"
    assert player.current_speed == 4


# ==================== E. 死亡流程 ====================

def test_e_unified_death_pipeline_from_hp_zero():
    """HP=0 → 统一命零判定 → ENTITY_DIED → 死后效果（焦黑发丝 / 尸体登记）。"""
    state, combat, player, enemy = _arena(enemy_hp=10)
    state.relics = [Relic("焦黑发丝", "")]
    player.current_speed = 3

    detail = combat._apply_hostile_damage(enemy, 10, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 10, "tags": {"daowen"}, "event_id": "E-1",
    })

    assert detail["died"] is True and enemy.is_alive is False and enemy.current_hp == 0
    assert enemy._death_ctx["parent_event_id"] == "E-1"
    death_events = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(death_events) == 1 and death_events[0].target_name == "M"
    # 死后效果
    assert player.current_speed == 5
    assert enemy in state.dead_monsters


def test_e_departure_is_not_death():
    """离场（雕塑/癌变/还债/救赎）不是命零，统一判定不得把它算成死亡。"""
    state, combat, player, enemy = _arena(enemy_hp=30)
    combat._remove_from_combat(enemy, "雕塑")
    enemy.current_hp = 0

    assert combat._check_hp_zero_death(enemy) is False
    assert _events(combat, CombatEventType.ENTITY_DIED) == []
    assert enemy.is_departed is True


def test_e_monster_mutation_does_not_emit_a_death_event():
    """怪物异变超过50层仍存活，不能进入死亡管线或触发焦黑发丝。"""
    state, combat, player, enemy = _arena(enemy_hp=40)
    state.relics = [Relic("焦黑发丝", "")]
    player.current_speed = 1
    enemy.mutation_count = 49
    combat.pay_numeric_cost(enemy, "异变", 5, cost_context={
        "timing": "monster_action", "source": "原初1", "source_type": "evolution",
        "actor": enemy, "target": enemy, "mechanic": "cost", "subtype": "mutation",
        "amount": 5, "tags": {"active_payment"},
    })
    assert enemy.is_alive and enemy.current_hp == 40 and enemy.mutation_count == 54
    assert not hasattr(enemy, "_death_ctx")
    assert not _events(combat, CombatEventType.ENTITY_DIED)
    assert player.current_speed == 1


def test_e_non_monster_lost_still_uses_unified_death():
    """非怪物无队友时的迷失命零仍走统一死亡管线，带完整父事件上下文。"""
    state, combat, player, enemy = _arena(enemy_hp=40)
    player.mutation_count = 49
    combat.pay_numeric_cost(player, "异变", 5, cost_context={
        "timing": "player_action", "source": "封印5", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "mutation",
        "amount": 5, "tags": {"active_payment"},
    })
    assert not player.is_alive and player.current_hp == 0
    assert player._death_ctx["subtype"] == "collapse"
    assert player._death_ctx["source"] == "迷失"
    assert player._death_ctx["parent_event_id"] is not None
    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1
    assert enemy.is_alive


# ==================== F. Hook 顺序显式化 ====================

def test_f_hook_order_is_explicit_and_unchanged():
    """priority 只是把重构前的字面顺序固化，绝不允许改动相对次序。

    【加害】【龙鳞】均已迁移为声明式 Mechanism（priority 20/30），经
    MechanismHookAdapter 挂在原位置执行——本测试同时钉死"迁移不改变顺序"。
    """
    manager = CombatHookManager()
    order = [type(h).__name__ for h in manager.hooks()]
    assert order == [
        "MechanismHookAdapter",  # 龙族血脉乘区（priority 10）
        "MechanismHookAdapter",  # 【加害】迁移后：原 JiahaiHook 位置（priority 20）
        "MechanismHookAdapter",  # 【龙鳞】迁移后：原 LonglinHook 位置（priority 30）
        "MechanismHookAdapter",  # 【固执】也接入同一伤害相位（priority 40）
        "QianjingjiaHook",
        "BifenglingHook",
        "DamageRedirectionHook",
        "LethalMitigationHook",
        "AfterDamageEffectsHook",
    ]
    priorities = [getattr(h, "priority", 100) for h in manager.hooks()]
    assert priorities == sorted(priorities)
    adapter_priorities = [h.priority for h in manager.hooks()
                          if isinstance(h, MechanismHookAdapter)]
    assert adapter_priorities == [10, 20, 30, 40], "加害(20)先于龙鳞(30)，固执(40)位于其后"


def test_f_jiahai_before_longlin_is_rule_relevant():
    """钉死顺序敏感性：反过来结算会得到不同伤害。"""
    manager = CombatHookManager()

    class _State:
        def side_has(self, entity, name):
            return False

    target = Entity("T", "怪物", blood_limit=50, current_hp=50)
    target.add_status(StatusEffect("加害", value=2, remaining_rounds=-1, source="x"))
    target.add_status(StatusEffect("龙鳞", value=8, remaining_rounds=-1, source="x"))

    adapters = [h for h in manager.hooks() if isinstance(h, MechanismHookAdapter)]
    jiahai_adapter = next(a for a in adapters if a.mechanism.name == "加害")
    longlin_adapter = next(a for a in adapters if a.mechanism.name == "龙鳞")

    # 输入 5：加害先结算为 7，再被龙鳞削至 0。
    assert manager.apply_incoming_adjust(target, 5, "普通", None, _State()) == 0
    # 反过来先结算龙鳞，5 被压到 0；加害仍会继续调整中间值，结果为 2。
    # 这同时钉死“中间值归零不短路后续状态”的现行语义。
    reversed_result = jiahai_adapter.on_incoming_adjust(
        target, longlin_adapter.on_incoming_adjust(target, 5, "普通", None, _State()),
        "普通", None, _State())
    assert reversed_result == 2


def test_f_register_hook_respects_priority():
    manager = CombatHookManager()

    class _First:
        priority = 1

        def on_incoming_adjust(self, target, amount, damage_type, source, state):
            return amount

    hook = _First()
    manager.register_hook(hook)
    assert manager.hooks()[0] is hook
    manager.register_hook(hook)
    assert manager.hooks().count(hook) == 1


# ==================== G. 血限压迫不得留下 0HP 存活体 ====================

@pytest.mark.parametrize("daowen,calc", [
    ("瓦解", {"x": 10, "blood_limit_pct": 100}),
    # 2026-10-13：【逼债】删除（原 DM裁定D 负债口径随道纹移除，见下）。
])
def test_g_blood_limit_pressure_never_leaves_alive_at_zero_hp(daowen, calc):
    _, combat, player, enemy = _arena(enemy_hp=20, enemy_bl=20)
    enemy.shards = 0

    combat.apply_daowen_effect(daowen, dict(calc), player, enemy)

    assert not (enemy.current_hp <= 0 and enemy.is_alive), \
        f"{daowen} 把生命压到 0 却没有走统一命零判定"
    if enemy.current_hp <= 0:
        assert enemy._death_ctx["mechanic"] == "death"


# 2026-10-13：【逼债】删除，原两组回始负债用例（_bizhai 账本）已删。
# 注意：删逼债后怪物侧负债暂无生产者（赎金/洗劫均 min(avail) 不透支），
# 【还债】debt_bind 触发器暂时不可达，待用户裁定后续处理。
# ==================== 递归保险丝 ====================

def test_effect_chain_depth_fuse_exists_and_is_generous():
    _, combat, player, enemy = _arena()
    assert combat.MAX_EFFECT_CHAIN_DEPTH >= 32
    assert combat._effect_chain_depth == 0
    combat._apply_hostile_damage(enemy, 1, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "amount": 1,
    })
    assert combat._effect_chain_depth == 0, "链深计数必须在退出时归零"

    combat._effect_chain_depth = combat.MAX_EFFECT_CHAIN_DEPTH
    with pytest.raises(RecursionError):
        combat._apply_hostile_damage(enemy, 1, source=player)
    combat._effect_chain_depth = 0


def test_redirection_chain_terminates_and_keeps_parent():
    """伤害重定向（背负）：链会收敛，且重定向后的伤害挂在原伤害之下。"""
    state, combat, player, enemy = _arena(enemy_hp=100)
    friend = Entity("F", "朋友", blood_limit=50, current_hp=50)
    state.friends = [friend]
    # 2026-10-11【嫁祸】已删除（效果与遗物【贪婪契约】重复）。本用例考察的是
    # 「伤害重定向」这一架构性质，与具体哪张牌无关，故改由仍在的【背负】承载：
    # 背负者(F)替目标(P)承担下1次伤害——重定向链、父事件挂载口径完全相同。
    friend._beifu_left = 1
    friend._beifu_target = player.runtime_id
    friend.add_status(StatusEffect("背负", value=1, remaining_rounds=1, source="F"))

    detail = combat._apply_hostile_damage(player, 10, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"}, "event_id": "R-1",
    })

    assert player.current_hp == 100 and friend.current_hp == 40
    assert "redirected" in detail["ctx"]["tags"]
    assert detail["ctx"]["parent_event_id"] == "R-1"
    assert combat._effect_chain_depth == 0


# ==================== H. 正常战斗路径不得依赖 legacy fallback ====================

def test_h_full_battle_path_produces_no_legacy_context(tmp_path):
    """跑一段真实战斗：伤害/回复/失血/血限/命零记录里不允许出现 legacy 来源。

    兼容层（ctx=None → legacy_*）保留，但它只能服务尚未迁移的旁路调用；
    一旦正常战斗路径开始产出 legacy 上下文，说明有新代码绕过了 EffectContext。
    注意 _hp_loss_events / _speed_change_events / _blood_limit_events 都是**回合级**
    缓冲（回始/回终清空，供活血等效果消费），所以必须在回终之前采样。
    """
    from tests.setup_support import begin_battle, begin_round, finish_initial_daowen
    from tests.attack_support import resolve_attack
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "chain_e2e.db"), rng_seed=7)
    engine.execute_action("setup_attributes", {
        "name": "审计者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 0
    assert begin_battle(engine)["success"]

    combat = engine.combat
    player = engine.state.player
    player.attack_count, player.attack_power = 2, 9

    def _entities():
        return ([player] + engine.state.friends + engine.state.employees
                + engine.state.temp_friends + engine.state.enemies)

    records = []

    def _sample():
        for ent in _entities():
            for attr in ("_hp_loss_events", "_heal_events",
                         "_speed_change_events", "_blood_limit_events"):
                records.extend(getattr(ent, attr, []) or [])

    for _ in range(2):
        assert begin_round(engine)["success"]
        assert resolve_attack(engine)["success"]
        engine.execute_action("use_daowen",
                              {"daowen_name": "杀伐", "x": 2, "target_ref": "enemy:0"})
        _sample()
        engine.state.combat_subphase = "await_round_end"
        engine.execute_action("round_end", {})
        if not engine.state.get_all_enemy_side():
            break

    assert records, "这一场应当至少产生若干可追溯的状态变化"
    offenders = [r for r in records if r.get("source_type") == "legacy"
                 or "legacy_context" in (r.get("tags") or [])]
    assert not offenders, f"正常战斗路径出现 legacy fallback：{offenders[:3]}"
    # 每条记录都能追回一个父事件或本身就是链根
    assert all(r.get("event_id") for r in records)

    for ent in _entities():
        death_ctx = getattr(ent, "_death_ctx", None)
        if death_ctx:
            assert death_ctx["source_type"] != "legacy", f"{ent.name} 的死亡没有来源"

    # 事件流确实被写入，并且每条事件都带得回上下文
    assert combat.event_stream, "CombatEvent 事件流不应为空"
    assert combat.event_stream is engine.state.combat_events
    for evt in combat.event_stream:
        assert evt.ctx is not None and evt.ctx.get("event_id")
    damage_events = [e for e in combat.event_stream
                     if e.event_type == CombatEventType.DAMAGE_APPLIED]
    assert damage_events and all(e.actor_name and e.target_name for e in damage_events)


def test_h_battle_start_resets_event_stream(tmp_path):
    """事件流按场重置，长时间模拟不会无界增长。"""
    from tests.setup_support import begin_battle, finish_initial_daowen
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "chain_reset.db"), rng_seed=3)
    engine.execute_action("setup_attributes", {
        "name": "审计者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 0
    assert begin_battle(engine)["success"]

    enemy = engine.state.enemies[0]
    engine.combat._apply_hostile_damage(enemy, 3, source=engine.state.player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": engine.state.player, "target": enemy, "mechanic": "damage", "amount": 3})
    assert engine.combat.event_stream

    engine.state.enemies = []
    engine.state.energy = 0
    engine.state.phase = "pre_battle"
    assert begin_battle(engine)["success"]
    assert engine.combat.event_stream == []


# ==================== I. 【活血衣】：受到攻击伤害后回复一半 ====================
# 2026-10-03：【活血】道纹删除；改为通用遗物【活血衣】
# （你受到攻击造成的伤害后，恢复等同其一半的生命，向上取整）。

def _huoxueyi_arena(player_hp=100):
    state, combat, player, enemy = _arena(player_hp=player_hp, enemy_hp=200, enemy_bl=200)
    state.relics.append(Relic(name="活血衣", effect=""))
    return state, combat, player, enemy


def _total_healed(player):
    return player.total_healed


def test_i_huoxueyi_heals_half_of_attack_damage():
    """攻击伤害 20 → 立即回复 10（向上取整）。"""
    state, combat, player, enemy = _huoxueyi_arena(player_hp=100)
    healed_before = _total_healed(player)

    combat._apply_hostile_damage(player, 20, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 20, "tags": {"attack"}, "event_id": "HX-1",
    })

    assert player.current_hp == 90, "20 伤 - 10 回复"
    assert _total_healed(player) - healed_before == 10, "回复量=伤害一半"
    assert player.hp_lost_this_round == 20, "失血统计不受回复影响"


def test_i_huoxueyi_rounds_up_on_odd_damage():
    """奇数伤害向上取整：9 → 回复 5。"""
    state, combat, player, enemy = _huoxueyi_arena(player_hp=100)
    healed_before = _total_healed(player)
    combat._apply_hostile_damage(player, 9, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 9, "tags": {"attack"},
    })
    assert _total_healed(player) - healed_before == 5
    assert player.current_hp == 96


def test_i_huoxueyi_does_not_trigger_on_daowen_damage():
    """非攻击伤害（道纹）不触发活血衣。"""
    state, combat, player, enemy = _huoxueyi_arena(player_hp=100)
    healed_before = _total_healed(player)
    combat._apply_hostile_damage(player, 20, source=enemy, ctx={
        "timing": "monster_action", "source": "杀伐", "source_type": "daowen",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"},
    })
    assert _total_healed(player) - healed_before == 0
    assert player.current_hp == 80


def test_i_huoxueyi_does_not_trigger_without_actual_hp_loss():
    """护盾完全吸收 → 实际伤害 0，不回复。"""
    state, combat, player, enemy = _huoxueyi_arena(player_hp=100)
    player.shield = 50
    healed_before = _total_healed(player)
    detail = combat._apply_hostile_damage(player, 20, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 20, "tags": {"attack"},
    })
    assert detail["shield_absorbed"] == 20 and detail["actual_damage"] == 0
    assert _total_healed(player) - healed_before == 0
    assert player.current_hp == 100


def test_i_huoxueyi_not_triggered_without_relic():
    """不持有遗物则无回复。"""
    state, combat, player, enemy = _arena(player_hp=100, enemy_hp=200, enemy_bl=200)
    healed_before = _total_healed(player)
    combat._apply_hostile_damage(player, 20, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 20, "tags": {"attack"},
    })
    assert _total_healed(player) - healed_before == 0
    assert player.current_hp == 80


# ==================== J. 二次验收补充回归 ====================

# 2026-10-03：【尸爆】道纹删除，其自毁式[命零]回归用例一并移除。


def test_j_entity_died_event_carries_actor_name_from_dict_context():
    """死亡上下文经 to_dict() 降级成名字字符串后，事件仍要带得出 actor_name。"""
    state, combat, player, enemy = _arena(enemy_hp=10)
    combat._apply_hostile_damage(enemy, 30, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 30, "tags": {"daowen"}, "event_id": "AN-1",
    })
    died = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(died) == 1
    assert died[0].actor_name == "P" and died[0].target_name == "M"
    assert died[0].parent_event_id == "AN-1"


def test_j_redirected_damage_emits_exactly_one_damage_event():
    """伤害重定向（背负）不得产生两条 DAMAGE_APPLIED（重定向在落地前 return）。"""
    state, combat, player, enemy = _arena()
    friend = Entity("F", "朋友", blood_limit=50, current_hp=50)
    state.friends = [friend]
    # 2026-10-11【嫁祸】已删除（效果与遗物【贪婪契约】重复）。本用例考察的是
    # 「伤害重定向」这一架构性质，与具体哪张牌无关，故改由仍在的【背负】承载：
    # 背负者(F)替目标(P)承担下1次伤害——重定向链、父事件挂载口径完全相同。
    friend._beifu_left = 1
    friend._beifu_target = player.runtime_id
    friend.add_status(StatusEffect("背负", value=1, remaining_rounds=1, source="F"))

    combat._apply_hostile_damage(player, 10, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"},
    })
    applied = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(applied) == 1 and applied[0].target_name == "F"


def test_j_purify_negative_mutation_never_collapses():
    """净化（负异变）不得因为 collapsed 判定而误杀。"""
    state, combat, player, enemy = _arena()
    enemy.mutation_count = 20
    combat.apply_daowen_effect("净化", {"x": 5, "mutation_reduction": 5}, player, enemy)
    assert enemy.mutation_count == 15 and enemy.is_alive is True
    assert _events(combat, CombatEventType.ENTITY_DIED) == []
