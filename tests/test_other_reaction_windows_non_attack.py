"""修复验证：非攻击伤害/失血路径也会真实触发其余反应型时点。

背景：受到伤害前 / 受到伤害后 / 失去生命前 这三个反应时点此前只在 resolve_attack
的普通攻击路径由显式窗口结算；同一持有者因道纹伤害 / 流血代价 / 直接失血等原因
降低生命时，这些反应法术（先发制人 / 护佑 / 亡语等）完全不会触发。用户要求：
只检测“该时点事件发生”就触发，不关心具体成因，也不逐个效果开窗。

本测试锁定统一入口 `_fire_auto_reaction` 对非攻击路径的契约：
  1. 道纹伤害（_apply_hostile_damage，非攻击）会触发 受到伤害前 / 受到伤害后 /
     失去生命前 / 失去生命后 四个窗口，且日志挂在 detail["reaction_logs"]。
  2. 流血代价（pay_numeric_cost）会触发 失去生命前。
  3. 直接失血（_raw_hp_loss）会触发 失去生命前。
  4. 普通攻击路径由 resolve_attack 的显式窗口结算，不重复双发通用入口。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, DaoWen, DaoWenInstance, Spell


def _state(player_mana: int = 20) -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions", current_round=1)
    player = Entity("玄夜", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=player_mana)
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100, attack_power=3)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _add_spell(player: Entity, daowen_name: str, trigger: str, flow: str) -> None:
    player.dao_wen[daowen_name] = DaoWenInstance(
        DaoWen(name=daowen_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=0)
    player.spells.append(Spell(
        name=f"{daowen_name}-{trigger}", required_daowen=[daowen_name],
        trigger_condition=trigger, effect_flow=flow))


def _daowen_ctx(actor, target, amount: int = 8) -> dict:
    return {"timing": "monster_action", "source": "杀伐", "source_type": "daowen",
            "actor": actor, "target": target, "mechanic": "damage", "subtype": "daowen",
            "amount": amount, "tags": {"daowen"}}


def _has_execution(detail: dict, spell: str) -> bool:
    for lg in (detail.get("reaction_logs") or []):
        if lg.get("spell") == spell and lg.get("execution"):
            return True
    return False


def test_before_damage_taken_fires_on_daowen_damage():
    """受到伤害前：敌方道纹对玩家造伤时，先发制人（杀伐→攻击者）自动反击。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "杀伐", "受到伤害前", "发动杀伐 X于攻击者")
    enemy_hp_before = enemy.current_hp
    detail = combat._apply_hostile_damage(player, 8, source=enemy, ctx=_daowen_ctx(enemy, player))
    assert _has_execution(detail, "杀伐-受到伤害前")
    assert enemy.current_hp < enemy_hp_before, "受到伤害前应触发反击"


def test_after_damage_taken_fires_on_daowen_damage():
    """受到伤害后：敌方道纹对玩家造伤落地后，护佑（庇护→自身）获得免疫次数。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "庇护", "受到伤害后", "发动庇护 X于自身")
    detail = combat._apply_hostile_damage(player, 8, source=enemy, ctx=_daowen_ctx(enemy, player))
    assert _has_execution(detail, "庇护-受到伤害后")
    assert player.get_status_value("庇护") > 0, "受到伤害后应获得庇护免疫次数（非格挡）"


def test_before_life_lost_fires_on_daowen_damage():
    """失去生命前：敌方道纹对玩家造伤但生命尚未扣减时，亡语（杀伐→攻击者）反击。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "杀伐", "失去生命前", "发动杀伐 X于攻击者")
    enemy_hp_before = enemy.current_hp
    detail = combat._apply_hostile_damage(player, 8, source=enemy, ctx=_daowen_ctx(enemy, player))
    assert _has_execution(detail, "杀伐-失去生命前")
    assert enemy.current_hp < enemy_hp_before, "失去生命前应触发反击"


def test_after_life_lost_fires_on_daowen_damage():
    """失去生命后：敌方道纹对玩家造伤后，生生不息（再生→自身）治疗并回写事件日志。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "再生", "失去生命后", "发动再生 X于自身")
    combat._apply_hostile_damage(player, 8, source=enemy, ctx=_daowen_ctx(enemy, player))
    # 失去生命后日志挂在 _hp_loss_events 的 reaction_logs（与其余窗口的 detail 挂接不同）。
    fired = any(
        lg.get("spell") == "再生-失去生命后" and lg.get("execution")
        for ev in (getattr(player, "_hp_loss_events", []) or [])
        for lg in (ev.get("reaction_logs") or []))
    assert fired
    # 再生治疗把生命拉回血限上限（100）。
    assert player.current_hp == 100


def test_before_life_lost_fires_on_bleed_cost():
    """流血代价（pay_numeric_cost）降低生命 → 失去生命前触发并回写日志。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "再生", "失去生命前", "发动再生 X于自身")
    detail = combat.pay_numeric_cost(player, "流血", 5, cost_context={
        "timing": "player_action", "source": "连击", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "bleed",
        "amount": 5, "tags": {"active_payment"},
    })
    # 失去生命前日志挂在 pay_numeric_cost 返回的 owner["detail"]["reaction_logs"] 上。
    owner = detail.get("owner") or {}
    assert _has_execution(owner, "再生-失去生命前") or _has_execution(
        owner.get("detail") or {}, "再生-失去生命前")


def test_before_life_lost_fires_on_raw_hp_loss():
    """直接失血（_raw_hp_loss）→ 失去生命前触发并回写日志，生命照常扣减。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "再生", "失去生命前", "发动再生 X于自身")
    result = combat._raw_hp_loss(player, 6)
    assert result["lost"] == 6
    assert _has_execution(result, "再生-失去生命前")


def test_windows_do_not_double_fire_on_attack():
    """普通攻击由 resolve_attack 显式窗口结算；通用入口不重复双发「失去生命后」。"""
    _, combat, player, enemy = _state()
    _add_spell(player, "再生", "失去生命后", "发动再生 X于自身")
    refs = combat._combat_entity_refs()
    spell_choices = {
        "before": {},
        "after": {"再生-失去生命后": {"use": True,
                                "steps": [{"x": 1, "target_ref": "player:0", "dodge": False}]}},
    }
    res = combat.resolve_attack(enemy, player, spell_choices=spell_choices, entity_refs=refs)
    # 窗口结算了一次生生不息。
    window_fired = any(
        lg.get("spell") == "再生-失去生命后" and lg.get("execution")
        for lg in (res.get("spell_logs") or []))
    assert window_fired
    # 攻击失血由 resolve_attack 的显式窗口接管；通用入口不再往 hp_loss_ctx 挂 reaction_logs，
    # 从而避免同一事件双发。
    hlc = getattr(player, "_hp_loss_events", [])[-1] if getattr(player, "_hp_loss_events", []) else {}
    assert "reaction_logs" not in hlc


def _duel_state(enemy_speed: int):
    """先发制人反打场景：玩家持【受到伤害前→杀伐于攻击者】，敌方有速度可闪避。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions", current_round=1)
    player = Entity("玄夜", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=20)
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100,
                   attack_power=3, speed_limit=enemy_speed, current_speed=enemy_speed)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def test_reaction_spell_counter_can_be_dodged():
    """DM 裁定（2026-08-31）：法术只是自定义触发条件的道纹，道纹要遵守的规则法术一样要遵守。

    规则正文「凡带 [目标] 道纹，目标被选定时均可消耗 1 点当前速度进行闪避」、
    规则正文「禁止跳过闪避判定」。【杀伐】带 [目标] 且不带必中，因此【先发制人】
    的杀伐反打**必须**给被选定方闪避窗口。

    修复前 `_auto_after_life_lost_decision` 把 dodge 写死 False，道纹伤害触发的反打
    无法闪避（死斗里表现为「先出手者必死」）。本用例锁定反打现在可被闪避。
    """
    _, combat, player, enemy = _duel_state(enemy_speed=5)
    _add_spell(player, "杀伐", "受到伤害前", "发动杀伐 X于攻击者")
    speed_before, hp_before = enemy.current_speed, enemy.current_hp

    combat._apply_hostile_damage(player, 8, source=enemy, ctx=_daowen_ctx(enemy, player))

    assert enemy.current_speed == speed_before - 1, "高伤反打应被闪避，消耗 1 点速度"
    assert enemy.current_hp == hp_before, "闪避成功后反打结算完全失效"


def test_reaction_spell_counter_lands_when_target_has_no_speed():
    """对照：无速度则不得闪避，反打照常落地（防止修成「一律闪避」）。"""
    _, combat, player, enemy = _duel_state(enemy_speed=0)
    _add_spell(player, "杀伐", "受到伤害前", "发动杀伐 X于攻击者")
    hp_before = enemy.current_hp

    combat._apply_hostile_damage(player, 8, source=enemy, ctx=_daowen_ctx(enemy, player))

    assert enemy.current_speed == 0, "速度为 0 不得闪避"
    assert enemy.current_hp < hp_before, "未闪避则反打应当造成伤害"
