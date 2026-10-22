"""修复验证：『失去生命后』(AFTER_LIFE_LOST) 反应法术对任意原因的生命下降触发。

背景：此前只有 resolve_attack 的普通攻击会打开『失去生命后』窗口；同一持有者
因道纹伤害 / 流血代价 / 直接失血 / 血限压迫等原因降低生命时，生生不息 / 以牙还牙
等反应法术完全不会触发。用户要求：只检测生命下降就触发，不关心成因。

本测试锁定修复后的契约：
  1. 非攻击失血（道纹伤害、流血代价、直接 _raw_hp_loss）都会触发生生不息。
  2. 普通攻击路径由 resolve_attack 的显式反应窗口结算，不重复经 hook 双发。
  3. 法力不足（再生类消耗法力）时不触发、且不崩溃；连击类（流血代价、非法力）
     即使 0 法力也照常触发。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, DaoWen, DaoWenInstance, Spell


def _combat_with_reactions(player_mana: int) -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("玄夜", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=player_mana)
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    state.current_round = 1
    # 生生不息：失去生命后 → 再生 X（消耗法力）
    player.dao_wen["再生"] = DaoWenInstance(
        DaoWen(name="再生", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0)
    player.spells.append(Spell(
        name="生生不息", required_daowen=["再生"],
        trigger_condition="失去生命后", effect_flow="失去生命后→发动再生 X"))
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _fired_in_hp_loss(player: Entity) -> bool:
    events = getattr(player, "_hp_loss_events", []) or []
    for e in events:
        for lg in (e.get("reaction_logs") or []):
            if lg.get("spell") == "生生不息" and lg.get("execution"):
                return True
    return False


def test_after_life_lost_fires_on_daowen_damage():
    """道纹（非攻击）伤害造成生命下降 → 生生不息触发。"""
    _, combat, player, enemy = _combat_with_reactions(player_mana=20)
    combat._apply_hostile_damage(player, 4, source=enemy, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "daowen",
        "amount": 4, "tags": {"daowen"},
    })
    # 4伤后触发再生（可支付上限 X=20，治疗溢出被血限压回满）。
    assert player.current_hp == 100
    assert _fired_in_hp_loss(player)


def test_after_life_lost_fires_on_bleed_cost():
    """流血代价降低生命 → 生生不息触发。"""
    _, combat, player, _ = _combat_with_reactions(player_mana=20)
    combat.pay_numeric_cost(player, "流血", 5, cost_context={
        "timing": "player_action", "source": "连击", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "bleed",
        "amount": 5, "tags": {"active_payment"},
    })
    assert _fired_in_hp_loss(player)


def test_after_life_lost_fires_on_raw_hp_loss_legacy():
    """直接 _raw_hp_loss（无 ctx/legacy） → 生生不息触发且仍保留 legacy 上下文契约。"""
    _, combat, player, _ = _combat_with_reactions(player_mana=20)
    result = combat._raw_hp_loss(player, 6)
    assert result["hp_loss_ctx"]["mechanic"] == "hp_loss"
    assert result["hp_loss_ctx"]["tags"] == ["legacy_context"]
    assert _fired_in_hp_loss(player)


def test_after_life_lost_no_double_fire_on_attack():
    """普通攻击失血由 resolve_attack 的反应窗口结算，hook 不重复双发。"""
    _, combat, player, enemy = _combat_with_reactions(player_mana=20)
    refs = combat._combat_entity_refs()
    spell_choices = {
        "before": {},
        "after": {"生生不息": {"use": True,
                            "steps": [{"x": 1, "target_ref": "player:0", "dodge": False}]}},
    }
    res = combat.resolve_attack(enemy, player, spell_choices=spell_choices, entity_refs=refs)
    # 窗口结算一次：生生不息执行
    fired_in_window = any(
        lg.get("spell") == "生生不息" and lg.get("execution")
        for lg in (res.get("spell_logs") or []))
    assert fired_in_window
    # hook 未在攻击目标的 hp_loss_ctx 上重复挂 reaction_logs
    hlc = getattr(player, "_hp_loss_events", [])[-1] if getattr(player, "_hp_loss_events", []) else {}
    assert "reaction_logs" not in hlc


def test_after_life_lost_insufficient_mana_does_not_fire_or_crash():
    """法力不足时，消耗法力的生生不息自动放弃触发（use=False），不崩溃、无副作用。"""
    _, combat, player, _ = _combat_with_reactions(player_mana=0)
    result = combat._raw_hp_loss(player, 6)
    assert result["lost"] == 6
    assert not _fired_in_hp_loss(player)
    assert player.current_hp == 94  # 只有扣血，没有治疗


def test_after_life_lost_blood_debt_fires_at_zero_mana():
    """连击（流血代价，不消耗法力）反应即使 0 法力也触发。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("青梧", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=0)
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    state.current_round = 1
    player.dao_wen["连击"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="流血", cost_formula="X", effect_formula=""),
        x_value=0)
    player.spells.append(Spell(
        name="不死不休", required_daowen=["连击"],
        trigger_condition="失去生命后", effect_flow="失去生命后→发动连击 X"))
    combat = CombatEngine(state, DiceEngine())

    # 敌方道纹造伤（0 法力、明确失血来源），不死不休的连击步无需法力即可反击。
    combat._apply_hostile_damage(player, 6, source=enemy, ctx={
        "timing": "monster_action", "source": "杀伐", "source_type": "daowen",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "daowen",
        "amount": 6, "tags": {"daowen"},
    })
    events = getattr(player, "_hp_loss_events", []) or []
    fired = any(
        lg.get("spell") == "不死不休" and lg.get("daowen") == "连击"
        for e in events for lg in (e.get("reaction_logs") or []))
    assert fired, "0 法力时连击类‘失去生命后’反应应照常触发（连击=流血代价，不耗法力）"
    # 连击 X=1：玩家自流 1 血并对敌方造成 1 伤；敌方仍存活。
    assert enemy.current_hp == 100 - 1
    assert player.current_hp == 100 - 6 - 1  # 6伤 + 连击自流1


def test_after_life_lost_fires_on_blood_limit_pressure():
    """血限压顶（血限降低 → 当前生命被压）→ 失去生命后触发。

    这条路径不经过伤害管线，走 Entity.__setattr__ 的兜底钩子；高血限 + 小额再生
    避免触发【癌变】（累计恢复 ≥ 血限×2 时命零）干扰断言。
    """
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("玄夜", "轮回者", blood_limit=1000, current_hp=1000,
                    mana_limit=20, current_mana=6)
    enemy = Entity("靶怪", "怪物", blood_limit=1000, current_hp=1000, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    state.current_round = 1
    player.dao_wen["再生"] = DaoWenInstance(
        DaoWen(name="再生", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0)
    player.spells.append(Spell(
        name="生生不息", required_daowen=["再生"],
        trigger_condition="失去生命后", effect_flow="失去生命后→发动再生 X"))
    combat = CombatEngine(state, DiceEngine())

    combat._apply_blood_limit_change(player, -6, "连击", "debuff", ctx={
        "timing": "player_action", "source": "连击", "source_type": "daowen",
        "actor": enemy, "target": player, "mechanic": "blood_limit",
        "subtype": "pressure", "amount": 6, "tags": {"daowen", "blood_limit_loss"},
    })
    # 血限压到 994，当前生命被压到 994；再生回复后回到血限上限。
    assert player.blood_limit == 994
    assert player.current_hp == player.blood_limit
    assert _fired_in_hp_loss(player), "血限压顶导致的生命下降应触发生生不息"


def test_after_life_lost_fires_on_direct_current_hp_write():
    """直接写入 current_hp（绕过伤害/代价管线）→ 兜底钩子也触发失去生命后。

    这是“只检测生命下降、不关心成因”的最终兜底：任何让 current_hp 变小的写法
    都会经 __setattr__ 上报，生成可见的失血事件。小额再生避免【癌变】干扰。
    """
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("玄夜", "轮回者", blood_limit=1000, current_hp=1000,
                    mana_limit=20, current_mana=6)
    enemy = Entity("靶怪", "怪物", blood_limit=1000, current_hp=1000, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    state.current_round = 1
    player.dao_wen["再生"] = DaoWenInstance(
        DaoWen(name="再生", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0)
    player.spells.append(Spell(
        name="生生不息", required_daowen=["再生"],
        trigger_condition="失去生命后", effect_flow="失去生命后→发动再生 X"))
    combat = CombatEngine(state, DiceEngine())

    # 直接降血（模拟未来新增效果/遗物不经任何既有管线的场景）。
    player.current_hp = player.current_hp - 6
    assert _fired_in_hp_loss(player), "直接 current_hp 下降也应触发生生不息"
    # 再生把生命拉回血限上限，登帐一次 fallback_hp_loss。
    events = [ev for ev in getattr(player, "_hp_loss_events", []) if ev.get("subtype") == "fallback_hp_loss"]
    assert events, "兜底钩子应登记一笔 fallback_hp_loss 失血账"
