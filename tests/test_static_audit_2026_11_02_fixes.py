"""第二轮静态排查报告（2026-11-02）六项缺陷修复的回归测试。

对照报告 `内部/审查报告/静态排查报告-2026-11-02-法术引擎与触发时机.md` §4：

1. 修复1【唯一】：规则阻断走 `INTERRUPTED / DAOWEN_UNUSABLE`，不再进 `FAILED`；
   候选枚举（反应/时点/主动行动）同步剔除被锁死的道纹。
2. 修复2【血契】：回声支付使用 `trigger_blood_pact=False`，双向血契不再无界递归。
3. 修复3【豪夺】/【篡改】：状态与账本同生共死（遗忘=立即归还，流动=状态计时）；
   篡改随状态转移，控制权仍归原 controller。
4. 修复4【残韵】/【出土】：转换继承冷却/封印/冰冻，不能洗白刷新。
5. 修复5：四个伤害/失血时点按 completion_order → 发现序排序。
6. 修复6：删除【缄默】死分支、`spent_unique` 死字段、`BifenglingHook.on_dodge` 死代码。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat_hooks import BifenglingHook, CombatHookManager
from engine.daowen import DaoWenEngine
from engine.models import (DaoWen, DaoWenInstance, Entity, Relic, Spell,
                           StatusEffect)
from engine.spell_execution import ExecutionStatus, InterruptReason, StepStatus
from tests.test_daowen_wiring import _engine, _give, _monster
from tests.test_spell_executor_semantics import _cast, _give as _give_dw, _state


# ---------------------------------------------------------------------------
# 修复1：【唯一】
# ---------------------------------------------------------------------------

def test_unique_no_longer_raises_from_calc_layer():
    """计算层不再用异常表达规则阻断——resolve 只算 calc。"""
    state, combat, player, enemy = _state()
    _give_dw(player, "杀伐")
    player.add_status(StatusEffect(name="唯一", value=1, remaining_rounds=3, source="测试"))
    player.mark_daowen_used("杀伐")
    assert player.daowen_locked_by_unique("杀伐") is True
    calc = DaoWenEngine.resolve("杀伐", 1, target=enemy, caster=player)
    assert calc["dao_wen"] == "杀伐"


def test_unique_step_interrupts_spell_as_daowen_unusable():
    """法术中途撞上【唯一】：正常中断，前序步骤保留，绝不是 FAILED。"""
    state, combat, player, enemy = _state()
    _give_dw(player, "杀伐", "再生")
    player.add_status(StatusEffect(name="唯一", value=1, remaining_rounds=3, source="测试"))
    player.mark_daowen_used("再生")
    execution = _cast(combat, player, enemy,
                      "发动杀伐X于目标→发动再生X于自身",
                      [{"x": 1, "dodge": False}, {"x": 1}])
    assert execution.status == ExecutionStatus.INTERRUPTED
    assert execution.interrupt_reason == InterruptReason.DAOWEN_UNUSABLE
    assert [r.status for r in execution.results] == [
        StepStatus.COMPLETED, StepStatus.INTERRUPTED], execution.results
    assert "唯一" in execution.interrupt_detail
    assert enemy.current_hp < 100, "已结算的第一步必须保留，不回滚"


def test_unique_locked_daowen_not_offered_as_reaction():
    """被【唯一】锁死的道纹不再出现在反应法术候选里。"""
    state, combat, player, enemy = _state()
    _give_dw(player, "再生")
    player.spells = [Spell(name="回血术", required_daowen=["再生"],
                           trigger_condition="失去生命后",
                           effect_flow="发动再生X于自身", trigger="失去生命后")]
    assert "回血术" in combat._eligible_spell_flows(player, "失去生命后")
    player.mark_daowen_used("再生")
    player.add_status(StatusEffect(name="唯一", value=1, remaining_rounds=3, source="测试"))
    assert combat._eligible_spell_flows(player, "失去生命后") == {}
    assert player.can_use_daowen("再生") is False


def test_direct_use_reports_unique_block_instead_of_calc_failure():
    """直接发动：明确文案，且不是"道纹计算失败"通道。"""
    e = _engine("fix_unique_use")
    p = e.state.player
    _give(p, "杀伐")
    foe = _monster(e, "唯一靶怪", hp=100)
    p.mark_daowen_used("杀伐")
    p.add_status(StatusEffect(name="唯一", value=1, remaining_rounds=3, source="测试"))
    result = e.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target": foe.name, "dodge": False})
    assert result["success"] is False
    assert "唯一" in result["error"]
    assert "计算失败" not in result["error"]


# ---------------------------------------------------------------------------
# 修复2：【血契】回声不递归
# ---------------------------------------------------------------------------

def _blood_pact_holder(payer, target, multiplier=2):
    """给 payer 挂一份指向 target 的【血契】（与 apply 时的结构一致）。"""
    payer.add_status(StatusEffect(
        name="血契", value=multiplier, remaining_rounds=3,
        source=str(target.runtime_id),
        effect_data={"target_id": str(target.runtime_id), "source_daowen": "血契"}))


def test_blood_pact_echo_does_not_recurse():
    """双向血契下支付一次法力，只回声一次，不发生 RecursionError / 幂级数放大。"""
    state, combat, player, enemy = _state()
    player.current_mana = 50
    # 怪物面板：法限默认为攻击力，必须先抬上限再写当前值。
    enemy.mana_limit = 50
    enemy.current_mana = 50
    _blood_pact_holder(player, enemy, multiplier=2)
    _blood_pact_holder(enemy, player, multiplier=2)

    combat.pay_numeric_cost(player, "法力", 1, forced=True)

    assert player.current_mana == 49, player.current_mana
    assert enemy.current_mana == 48, (
        "回声只应扣一次 1×2；若发生链式外溢会变成 48-… 或直接 RecursionError")


def test_blood_pact_echo_still_forces_full_amount_once():
    """单向血契：目标按名义量的 X 倍整笔支付（不因递归锁而变成 0 或不付）。"""
    state, combat, player, enemy = _state()
    player.current_mana = 30
    enemy.mana_limit = 30
    enemy.current_mana = 30
    _blood_pact_holder(player, enemy, multiplier=3)
    combat.pay_numeric_cost(player, "法力", 2, forced=True)
    assert player.current_mana == 28
    assert enemy.current_mana == 24


# ---------------------------------------------------------------------------
# 修复3：【豪夺】账本与状态合一 / 【篡改】随状态转移
# ---------------------------------------------------------------------------

def _haoduo_setup(suffix):
    e = _engine("fix3_" + suffix)
    p = e.state.player
    _give(p, "豪夺")
    _give(p, "遗忘")
    _give(p, "流动")
    m = _monster(e, "豪夺靶怪" + suffix, hp=200)
    m.relics = [Relic("防弹插板", "伤害减半")]
    e.state.shards = 200
    e.state.player.current_mana = 200
    e.state.player.mana_limit = 200
    e.execute_action("round_start", {})
    assert e.combat._steal_one_relic(p, m, 3, "防弹插板") == "防弹插板"
    return e, p, m


def _haoduo_status(holder, relic_name="防弹插板"):
    return next((s for s in holder.status_effects
                 if s.name == "豪夺" and s.effect_data.get("relic_name") == relic_name), None)


def test_yiwang_returns_stolen_relic_immediately():
    """【遗忘】移除【豪夺】状态 → 遗物立即归还、账本立即清空。"""
    e, p, m = _haoduo_setup("yiwang")
    assert "防弹插板" in e.state.stolen_relics
    status = _haoduo_status(p)
    assert status is not None
    index = p.status_effects.index(status)

    calc = DaoWenEngine.resolve("遗忘", 1, target=p)
    e.combat.prepare_liudong_yiwang("遗忘", calc, p, p, {"status_indices": [index]})
    result = e.combat.apply_daowen_effect("遗忘", calc, p, p, cost_share_target_ref="")

    assert _haoduo_status(p) is None
    assert "防弹插板" not in e.state.stolen_relics, "账本必须同步清空"
    assert not any(r.name == "防弹插板" for r in e.state.relics), "遗物不能留在夺取方"
    assert any(r.name == "防弹插板" for r in m.relics), "必须立即还给原持有者"
    assert any(eff.get("type") == "statuses_removed" for eff in result["effects"])


def test_liudong_moves_haoduo_and_status_is_the_clock():
    """【流动】把【豪夺】搬到别人身上后，到期时间由状态自身回合数决定。"""
    e, p, m = _haoduo_setup("liudong")
    ally = Entity("同伴", "朋友", blood_limit=50, current_hp=50,
                  mana_limit=20, current_mana=20, speed_limit=3, current_speed=3)
    e.state.friends.append(ally)
    status = _haoduo_status(p)
    assert status.remaining_rounds == 3
    index = p.status_effects.index(status)

    calc = DaoWenEngine.resolve("流动", 1, target=p)
    e.combat.prepare_liudong_yiwang(
        "流动", calc, p, p,
        {"status_indices": [index], "flow_target_ref": f"friend:0"})
    e.combat.apply_daowen_effect("流动", calc, p, p, cost_share_target_ref="")

    moved = _haoduo_status(ally)
    assert moved is not None and moved is status, "搬的是同一个状态对象"
    assert _haoduo_status(p) is None
    assert "防弹插板" in e.state.stolen_relics, "遗物本体仍在夺取方，账本不清"

    # 回终：状态（在同伴身上）递减，账本跟着走
    e.combat._tick_sealed_and_stolen_relics()
    assert moved.remaining_rounds == 2
    assert e.state.stolen_relics["防弹插板"]["remaining"] == 2
    assert any(r.name == "防弹插板" for r in e.state.relics)

    # 递减到 0 → 归还
    e.combat._tick_sealed_and_stolen_relics()
    e.combat._tick_sealed_and_stolen_relics()
    assert "防弹插板" not in e.state.stolen_relics
    assert not any(r.name == "防弹插板" for r in e.state.relics)
    assert any(r.name == "防弹插板" for r in m.relics)


def test_huixiang_extension_of_moved_haoduo_still_delays_return():
    """被搬走的【豪夺】再吃【回响】：状态变长 = 归还延后（账本跟随状态）。"""
    e, p, m = _haoduo_setup("liudong_echo")
    _give(p, "回响")
    ally = Entity("同伴", "朋友", blood_limit=50, current_hp=50,
                  mana_limit=20, current_mana=20, speed_limit=3, current_speed=3)
    e.state.friends.append(ally)
    status = _haoduo_status(p)
    index = p.status_effects.index(status)
    calc = DaoWenEngine.resolve("流动", 1, target=p)
    e.combat.prepare_liudong_yiwang(
        "流动", calc, p, p,
        {"status_indices": [index], "flow_target_ref": "friend:0"})
    e.combat.apply_daowen_effect("流动", calc, p, p, cost_share_target_ref="")

    moved = _haoduo_status(ally)
    moved_index = ally.status_effects.index(moved)
    echo = DaoWenEngine.resolve("回响", 2, target=ally)
    e.combat.prepare_huixiang(echo, p, ally, {"status_index": moved_index})
    e.combat.apply_daowen_effect("回响", echo, p, ally, cost_share_target_ref="")

    assert moved.remaining_rounds == 5
    for _ in range(3):
        e.combat._tick_sealed_and_stolen_relics()
    assert moved.remaining_rounds == 2
    assert "防弹插板" in e.state.stolen_relics, "延长后不能提前归还"
    assert any(r.name == "防弹插板" for r in e.state.relics)


def test_liudong_moves_tamper_and_control_follows_status():
    """【篡改】被【流动】搬给新目标后，新目标发动道纹时由原控制者选目标。"""
    state, combat, player, enemy = _state()
    ally = Entity("同伴", "朋友", blood_limit=100, current_hp=100,
                  mana_limit=100, current_mana=100, speed_limit=5, current_speed=5)
    state.friends.append(ally)
    _give_dw(player, "流动", "篡改")
    rule = StatusEffect(name="篡改", value=2, remaining_rounds=-1, source=player.name,
                        controller_runtime_id=str(player.runtime_id))
    rule.effect_data["source_daowen"] = "篡改"
    ally.add_status(rule)
    ally_index = ally.status_effects.index(rule)

    # 施法者（player）把同伴身上的篡改搬给敌人：来源=同伴，接收=敌人
    calc = DaoWenEngine.resolve("流动", 1, target=ally)
    refs = combat._combat_entity_refs()
    enemy_ref = next(ref for ref, ent in refs.items() if ent is enemy)
    combat.prepare_liudong_yiwang(
        "流动", calc, player, ally,
        {"status_indices": [ally_index], "flow_target_ref": enemy_ref})
    combat.apply_daowen_effect("流动", calc, player, ally, cost_share_target_ref="")

    moved = next((s for s in enemy.status_effects if s.name == "篡改"), None)
    assert moved is rule, "搬的是同一个状态对象，控制器指针随之保留"
    assert moved.controller_runtime_id == str(player.runtime_id)

    # 新目标的 schema 里出现篡改决策，控制者仍是原施法者
    schema = combat.daowen_extra_choice_schema(enemy, "杀伐", refs)
    assert schema["tamper"]["controller_id"] == str(player.runtime_id)

    # 新目标发动道纹时，篡改真的生效（由 controller 指定目标）
    _give_dw(enemy, "杀伐")
    player_ref = next(ref for ref, ent in refs.items() if ent is player)
    tampered, used_rule = combat.resolve_tampered_target(
        enemy, player, "杀伐",
        {"tamper": {"controller_id": str(player.runtime_id), "target_ref": player_ref}},
        refs)
    assert used_rule is rule
    assert tampered is player


# ---------------------------------------------------------------------------
# 修复4：【残韵】/【出土】不得洗白冷却与冻结
# ---------------------------------------------------------------------------

def test_resonance_conversion_inherits_cooldown_and_freeze():
    """1换1转换：新实例继承冷却/按回合冷却/封印/冰冻。"""
    e = _engine("fix4_plain")
    p = e.state.player
    _give(p, "杀伐")
    old = p.dao_wen["杀伐"]
    old.cooldown_remaining = 2
    old.cooldown_rounds_remaining = 1
    old.is_frozen = True
    old.ice_frozen = True
    p.dao_wen["再生"] = DaoWenInstance(DaoWen(name="再生", formula="", cost_type="",
                                             cost_formula="", effect_formula=""))

    assert e._permanently_convert_daowen(p, "杀伐", "封印") is True
    assert "杀伐" not in p.dao_wen
    converted = p.dao_wen["封印"]
    assert converted.cooldown_remaining == 2
    assert converted.cooldown_rounds_remaining == 1
    assert converted.is_frozen is True
    assert converted.ice_frozen is True
    assert converted.can_use() is False, "转换出来的新道纹同样处于受限状态"


def test_excavation_keeps_source_untouched_and_derived_also_restricted():
    """出土生效：原道纹原样保留（冷却/冻结不变），衍生实例继承同一份受限状态。"""
    e = _engine("fix4_chutu")
    p = e.state.player
    _give(p, "杀伐")
    p.add_status(StatusEffect(name="出土", value=2, remaining_rounds=-1, source="测试"))
    old = p.dao_wen["杀伐"]
    old.cooldown_remaining = 3
    old.is_frozen = True

    assert e._permanently_convert_daowen(p, "杀伐", "再生") is True
    assert old is p.dao_wen["杀伐"], "出土保留原实例"
    assert old.cooldown_remaining == 3 and old.is_frozen is True
    derived = p.dao_wen["再生"]
    assert derived.cooldown_remaining == 3
    assert derived.is_frozen is True
    assert p.get_status_value("出土") == 1, "每次换名消耗 1 次出土"


# ---------------------------------------------------------------------------
# 修复5：四个伤害/失血时点按完成序号排序
# ---------------------------------------------------------------------------

def test_reaction_spells_follow_completion_order():
    """同一持有者的两个「失去生命后」法术：先完成者先结算，与写法顺序无关。"""
    state, combat, player, enemy = _state()
    _give_dw(player, player_hp := "再生") if False else _give_dw(player, "再生")
    later = Spell(name="后学会", required_daowen=["再生"],
                  trigger_condition="失去生命后", effect_flow="发动再生X于自身",
                  trigger="失去生命后", completion_order=2)
    earlier = Spell(name="先学会", required_daowen=["再生"],
                    trigger_condition="失去生命后", effect_flow="发动再生X于自身",
                    trigger="失去生命后", completion_order=1)
    # 写法顺序故意与完成顺序相反
    player.spells = [later, earlier]
    refs = combat._combat_entity_refs()
    logs = combat._resolve_spell_reactions(
        "失去生命后", player, enemy,
        {"后学会": {"use": True, "steps": [{"x": 1}]},
         "先学会": {"use": True, "steps": [{"x": 1}]}},
        refs)
    cast_order = [log.get("spell") for log in logs if log.get("spell")]
    assert cast_order[:2] == ["先学会", "后学会"], logs


# ---------------------------------------------------------------------------
# 修复6：死代码 / 死字段
# ---------------------------------------------------------------------------

def test_dead_code_and_fields_are_gone():
    assert "缄默" not in DaoWenEngine.list_all(), "【缄默】已遗物化，不再是道纹"
    instance = DaoWenInstance(DaoWen("杀伐", "", "", "", ""))
    assert not hasattr(instance, "spent_unique")
    assert not hasattr(BifenglingHook(), "on_dodge")
    manager = CombatHookManager()
    assert manager.apply_dodge(Entity("甲", "轮回者"), None) == {}
