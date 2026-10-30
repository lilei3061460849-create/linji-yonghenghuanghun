"""Phase 2：瞬发（trigger=瞬发/immediate）+ lifecycle 专项测试。

验收句：cast(flow=...) 是一次消耗 1 出手的 instant SpellExecution；它按提交的
步骤顺序逐步执行，每一步使用执行时真实资源状态，资源不足只中断当前法术且保留
已经结算的步骤，不产生持久 Binding。

所有断言只通过公开入口 execute_action("cast", {...}) 或
combat.build_instant_execution(...) 验证；不针对具体法术名写任何生产分支。
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.daowen import DaoWenEngine
from engine.models import Spell, StatusEffect
from engine.spell_dsl import (
    ALL_TRIGGERS, SpellDslError, TRIGGER_INSTANT, parse_instant_flow, parse_trigger,
)
from engine.models import DaoWen, DaoWenInstance
from engine.spell_execution import (
    ExecutionStatus, InterruptReason, Lifecycle, SpellExecution, StepResult, StepStatus,
    TriggerType,
)
from tests.setup_support import grant_extra_actions
from tests.test_dragon_heart import _new_engine, _start_with_enemy

# 返回 dict 里是 .value 字符串；这里取枚举值做断言，避免在测试里手写字面量。
EXECUTION_COMPLETED = ExecutionStatus.COMPLETED.value
EXECUTION_INTERRUPTED = ExecutionStatus.INTERRUPTED.value
LIFECYCLE_INSTANT = Lifecycle.INSTANT.value
LIFECYCLE_BATTLE = Lifecycle.BATTLE.value
LIFECYCLE_PERMANENT = Lifecycle.PERMANENT.value
STEP_COMPLETED = StepStatus.COMPLETED.value
STEP_INTERRUPTED = StepStatus.INTERRUPTED.value
REASON_DAOWEN_UNUSABLE = InterruptReason.DAOWEN_UNUSABLE.value
REASON_MANA_INSUFFICIENT = InterruptReason.MANA_INSUFFICIENT.value
REASON_SHARDS_INSUFFICIENT = InterruptReason.SHARDS_INSUFFICIENT.value


def _engine_with_daowens(suffix, daowens=("杀伐", "庇护", "再生", "透支"), mana=20):
    engine = _new_engine(suffix)
    player = engine.state.player
    for name in daowens:
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    _start_with_enemy(engine)
    foe = engine.state.enemies[0]
    foe.current_hp = 999
    foe.attack_power = 5
    foe.attack_count = 1
    foe.current_speed = 1
    player.current_hp = 50
    player.current_mana = mana
    player.current_speed = 2
    return engine


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _setup(suffix, mana=10):
    engine = _engine_with_daowens(suffix)
    player = engine.state.player
    player.mana_limit = 30
    player.current_mana = mana
    player.current_hp = 30
    # 2026-10-25 法术阶级改按「重数」：瞬发法术每步一重，3 步即 3 阶 = 3 次出手。
    # 夹具用【蓄锐·增】把预算补到 9 次出手，专测逐步结算语义。
    grant_extra_actions(player, 7)
    return engine


def _ref(engine, entity):
    refs = engine.combat._combat_entity_refs()
    return next(r for r, e in refs.items() if e is entity)


def _cast(engine, flow, steps, target=None, **extra):
    """旧语义测试显式让对方跳过所有空档，再检查完整执行结果。

    生产API每步后都会暂停；本辅助只在测试中提交真实skip行动，不让引擎代跳。
    """
    params = {"flow": flow, "steps": steps}
    if target is not None:
        params["target_ref"] = _ref(engine, target)
    params.update(extra)
    resp = engine.execute_action("cast", params)
    if not resp.get("success"):
        return resp
    while engine.combat._spell_stack:
        side = engine.state.duel_turn
        actor_ref = next(
            ref for ref, entity in engine.combat._combat_entity_refs().items()
            if entity.is_alive and engine.state.on_player_side(entity) == (side == "player_side"))
        skipped = engine.execute_action("skip", {"actor_ref": actor_ref})
        assert skipped.get("success"), skipped
        progress = skipped.get("spell_progress") or []
        if progress:
            latest = progress[-1]
            resp["result"].update({
                "step_results": latest["step_results"],
                "execution_status": latest["execution_status"],
                "interrupt_reason": latest["interrupt_reason"],
                "interrupt_detail": latest["interrupt_detail"],
            })
    return resp


def _statuses(resp):
    return [s["status"] for s in resp["result"]["step_results"]]


def _spell_snapshot(entity):
    return [s.to_dict() for s in entity.spells]


def _expected_damage(engine, x, target):
    calc = DaoWenEngine.resolve("杀伐", x, target=target, caster=engine.state.player)
    return calc


# ---------------------------------------------------------------------------
# Test 1：瞬发单步
# ---------------------------------------------------------------------------

def test_instant_single_step():
    engine = _setup("p2_single")
    player, foe = engine.state.player, engine.state.enemies[0]
    hp0, mana0, used0 = foe.current_hp, player.current_mana, player.actions_used_this_round
    snap0 = _spell_snapshot(player)

    resp = _cast(engine, "发动杀伐X于目标", [{"x": 2, "dodge": False}], target=foe)

    assert resp["success"], resp
    r = resp["result"]
    assert r["trigger"] == TRIGGER_INSTANT
    assert r["lifecycle"] == LIFECYCLE_INSTANT
    assert r["execution_status"] == EXECUTION_COMPLETED
    assert _statuses(resp) == [STEP_COMPLETED]
    # 伤害：落到目标身上的数值 = 执行日志里的实际伤害，且 >0
    dealt = sum(e.get("actual_damage", 0) for e in r["steps"][0]["execution"]["effects"]
                if e.get("type") == "damage")
    assert dealt > 0 and foe.current_hp == hp0 - dealt
    # 法力：杀伐 cost=X（与道纹引擎计算一致）
    calc = _expected_damage(engine, 2, foe)
    assert calc["cost_type"] == "消耗"
    assert player.current_mana == mana0 - calc["cost"] == mana0 - 2
    assert player.actions_used_this_round == used0 + 1
    assert _spell_snapshot(player) == snap0


# ---------------------------------------------------------------------------
# Test 2：瞬发双步，只消耗 1 出手
# ---------------------------------------------------------------------------

def test_instant_two_steps_consume_tier_actions():
    # 2026-10-11 对齐 法术索引.md「法术阶级规则」：施展X阶法术一次付清X次出手。
    # 本例2步=2种道纹=2阶，故扣2次（旧口径"瞬发只扣1次"已废）。
    engine = _setup("p2_two")
    player, foe = engine.state.player, engine.state.enemies[0]
    used0 = player.actions_used_this_round

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 1, "dodge": False}, {"x": 1}], target=foe)

    assert resp["success"], resp
    assert _statuses(resp) == [STEP_COMPLETED, STEP_COMPLETED]
    assert resp["result"]["execution_status"] == EXECUTION_COMPLETED
    assert player.actions_used_this_round == used0 + 2


def test_instant_three_steps_consume_tier_actions():
    engine = _setup("p2_three")
    player, foe = engine.state.player, engine.state.enemies[0]
    # 3阶施法要一次付清3次出手，基础预算只有2次 → 用【疯狂】+1 补足（正文[速限]修正项）
    player.add_status(StatusEffect(name="疯狂", remaining_rounds=1, value=1, source="test"))
    used0 = player.actions_used_this_round
    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身→发动庇护X于自身",
                 [{"x": 1, "dodge": False}, {"x": 1}, {"x": 1}], target=foe)
    assert resp["success"], resp
    assert len(resp["result"]["step_results"]) == 3
    # 3步=3种道纹=3阶 → 扣3次出手（旧口径"仍只扣1次"已废）
    assert player.actions_used_this_round == used0 + 3


# ---------------------------------------------------------------------------
# Test 3：每步使用自己的 X
# ---------------------------------------------------------------------------

def test_instant_each_step_uses_its_own_x(monkeypatch):
    engine = _setup("p2_x")
    player, foe = engine.state.player, engine.state.enemies[0]
    seen = []
    original = engine.combat._execute_single_daowen_step

    def spy(**kw):
        seen.append((engine.combat._step_daowen(kw["step"]), kw["entry"].x))
        return original(**kw)

    monkeypatch.setattr(engine.combat, "_execute_single_daowen_step", spy)
    mana0, hp0 = player.current_mana, player.current_hp

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 2, "dodge": False}, {"x": 3}], target=foe)

    assert resp["success"], resp
    assert seen == [("杀伐", 2), ("再生", 3)]
    sr = resp["result"]["step_results"]
    assert [s["x"] for s in sr] == [2, 3]
    assert [s["cost_paid"] for s in sr] == [2, 3]
    assert player.current_mana == mana0 - 5
    heal = DaoWenEngine.resolve("再生", 3, target=player, caster=player)
    assert player.current_hp > hp0  # 再生 3 实际回复
    assert heal["x"] == 3


# ---------------------------------------------------------------------------
# Test 4：第二步法力不足 → 第一步保留、第二步 interrupted、无 ValueError
# ---------------------------------------------------------------------------

def test_instant_second_step_mana_insufficient_keeps_first():
    engine = _setup("p2_second_short", mana=3)
    player, foe = engine.state.player, engine.state.enemies[0]
    hp_foe0, hp0 = foe.current_hp, player.current_hp

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 3, "dodge": False}, {"x": 2}], target=foe)

    assert resp["success"], resp  # 施法动作本身发生了；不是异常
    assert _statuses(resp) == [STEP_COMPLETED, STEP_INTERRUPTED]
    assert resp["result"]["step_results"][1]["reason"] == REASON_MANA_INSUFFICIENT
    assert resp["result"]["execution_status"] == EXECUTION_INTERRUPTED
    assert resp["result"]["interrupt_reason"] == REASON_MANA_INSUFFICIENT
    # 没有 rollback：伤害与扣费都保留
    assert foe.current_hp < hp_foe0
    assert player.current_mana == 0
    # 第二步未执行：没有回血
    assert player.current_hp == hp0


# ---------------------------------------------------------------------------
# Test 5：第一步法力不足 → 后续步骤不执行
# ---------------------------------------------------------------------------

def test_instant_first_step_mana_insufficient_stops():
    engine = _setup("p2_first_short", mana=1)
    player, foe = engine.state.player, engine.state.enemies[0]
    hp_foe0, hp0, used0 = foe.current_hp, player.current_hp, player.actions_used_this_round

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 3, "dodge": False}, {"x": 1}], target=foe)

    assert resp["success"], resp
    assert _statuses(resp) == [STEP_INTERRUPTED]  # 第二步根本没有结果
    assert resp["result"]["execution_status"] == EXECUTION_INTERRUPTED
    assert foe.current_hp == hp_foe0
    assert player.current_hp == hp0
    assert player.current_mana == 1
    # 逐步支付：只为已尝试的第一步付1出手，未开始的第二步不预付。
    assert player.actions_used_this_round == used0 + 1


# ---------------------------------------------------------------------------
# Test 6：instant 不污染角色
# ---------------------------------------------------------------------------

def test_instant_does_not_create_binding():
    engine = _setup("p2_nobind")
    player, foe = engine.state.player, engine.state.enemies[0]
    spells_before = copy.deepcopy([s.to_dict() for s in player.spells])

    for _ in range(2):
        player.actions_used_this_round = 0
        resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                     [{"x": 1, "dodge": False}, {"x": 1}], target=foe, name="试刃")
        assert resp["success"], resp

    assert [s.to_dict() for s in player.spells] == spells_before
    assert all(s.name != "试刃" for s in player.spells)
    assert "试刃" not in engine.combat._eligible_spell_flows(player, TRIGGER_INSTANT)
    # 存档序列化里也没有它
    assert "试刃" not in str(player.to_dict())


# ---------------------------------------------------------------------------
# Test 7：target resolver（自身/施法者/目标/任意目标）
# ---------------------------------------------------------------------------

def test_instant_target_roles_match_existing_semantics():
    engine = _setup("p2_roles")
    combat = engine.combat
    player, foe = engine.state.player, engine.state.enemies[0]
    refs = combat._combat_entity_refs()
    execution = combat.build_instant_execution(
        player, "发动杀伐X于目标→发动再生X于自身→发动庇护X于施法者→发动杀伐X于任意目标",
        foe, [{"x": 1, "dodge": False}, {"x": 1}, {"x": 1},
              {"x": 1, "target_ref": _ref(engine, foe), "dodge": False}], refs)
    assert execution.definition.trigger == TriggerType.IMMEDIATE
    assert execution.definition.lifecycle == Lifecycle.INSTANT
    from engine.spell_dsl import iter_action_steps
    program = list(iter_action_steps(execution.definition.body))
    resolved = [execution.target_resolver(s, {"target_ref": e.target_ref}, player, foe, refs)[0]
                for (_i, s), e in zip(program, execution.request.steps)]
    assert resolved == [foe, player, player, foe]
    # 与既有「目标发动道纹前」身份映射同一函数：目标→对方，自身/施法者→持有者
    assert combat._trigger_spell_subject("target", player, foe) == "actor"
    assert combat._trigger_spell_subject("self", player, foe) == "holder"
    assert combat._trigger_spell_subject("caster", player, foe) == "holder"
    # 执行后真实落点一致
    execution.run_all()
    assert [r.target_name for r in execution.results] == [foe.name, player.name, player.name, foe.name]
    assert execution.status == ExecutionStatus.COMPLETED


def test_instant_flying_target_rejected_without_consuming_action():
    engine = _setup("p2_fly")
    player, foe = engine.state.player, engine.state.enemies[0]
    foe.is_flying = True
    used0 = player.actions_used_this_round
    resp = _cast(engine, "发动杀伐X于目标", [{"x": 1, "dodge": False}], target=foe)
    assert not resp["success"]
    assert player.actions_used_this_round == used0


def test_instant_attacker_role_rejected():
    with pytest.raises(SpellDslError):
        parse_instant_flow("发动杀伐X于攻击者", set(DaoWenEngine.list_all()))


# ---------------------------------------------------------------------------
# 重点压力测试：逐步执行 + 逐步资源检查（无总费用预检、无回滚、无异常）
# ---------------------------------------------------------------------------

def test_instant_stress_three_steps_third_interrupted():
    # 杀伐2(2) + 再生3(3) = 5 刚好付得起前两步；第三步杀伐1 需 1 → 不足
    engine = _setup("p2_stress", mana=5)
    player, foe = engine.state.player, engine.state.enemies[0]
    hp_foe0, hp0 = foe.current_hp, player.current_hp

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身→发动杀伐X于目标",
                 [{"x": 2, "dodge": False}, {"x": 3}, {"x": 1, "dodge": False}], target=foe)

    assert resp["success"], resp  # 没有因为总费用 6>5 在提交时被整体拒绝
    assert _statuses(resp) == [STEP_COMPLETED, STEP_COMPLETED, STEP_INTERRUPTED]
    assert resp["result"]["step_results"][2]["reason"] == REASON_MANA_INSUFFICIENT
    assert resp["result"]["execution_status"] == EXECUTION_INTERRUPTED
    assert foe.current_hp < hp_foe0 and player.current_hp > hp0  # 前两步保留
    assert player.current_mana == 0


def test_instant_second_step_reads_real_mana_after_first():
    # 0 法力起手：透支（流血代价）先产法力，杀伐再花掉——只有逐步读取真实法力才能成立
    engine = _setup("p2_rolling", mana=0)
    player, foe = engine.state.player, engine.state.enemies[0]
    hp_foe0 = foe.current_hp
    resp = _cast(engine, "发动透支X于自身→发动杀伐X于目标",
                 [{"x": 2}, {"x": 2, "dodge": False}], target=foe)
    assert resp["success"], resp
    assert _statuses(resp) == [STEP_COMPLETED, STEP_COMPLETED]
    assert resp["result"]["step_results"][0]["mana_gained"] == 2
    assert foe.current_hp < hp_foe0
    assert player.current_mana == 0


def test_execution_interrupts_when_daowen_becomes_unusable():
    engine = _setup("p2_unusable")
    combat = engine.combat
    player, foe = engine.state.player, engine.state.enemies[0]
    refs = combat._combat_entity_refs()
    execution = combat.build_instant_execution(
        player, "发动杀伐X于目标→发动再生X于自身", foe,
        [{"x": 1, "dodge": False}, {"x": 1}], refs)
    # 模拟第一步之后再生被封印：在执行期由 Execution 自己检测为正常中断
    original = combat._execute_single_daowen_step

    def freeze_after_first(**kw):
        r = original(**kw)
        player.dao_wen["再生"].is_frozen = True
        return r

    combat._execute_single_daowen_step = freeze_after_first
    try:
        execution.run_all()
    finally:
        del combat._execute_single_daowen_step
    assert [r.status for r in execution.results] == [StepStatus.COMPLETED, StepStatus.INTERRUPTED]
    assert execution.results[1].reason == InterruptReason.DAOWEN_UNUSABLE
    assert execution.status == ExecutionStatus.INTERRUPTED


def test_instant_routes_through_single_step_core(monkeypatch):
    engine = _setup("p2_core")
    foe = engine.state.enemies[0]
    calls = []
    original = engine.combat._execute_single_daowen_step

    def spy(**kw):
        r = original(**kw)
        assert isinstance(r, StepResult)
        calls.append(engine.combat._step_daowen(kw["step"]))
        return r

    monkeypatch.setattr(engine.combat, "_execute_single_daowen_step", spy)
    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 1, "dodge": False}, {"x": 1}], target=foe)
    assert resp["success"]
    assert calls == ["杀伐", "再生"]


# ---------------------------------------------------------------------------
# 契约错误：非法提交直接拒绝，不扣出手，不执行任何步骤
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flow,steps,why", [
    ("发动杀伐X于目标→循环", [{"x": 1, "dodge": False}], "max_iterations 非整数"),
    ("发动杀伐X于目标→循环", [{"x": 1, "dodge": False}], "max_iterations 超安全阀"),
    ("发动杀伐X于目标→发动再生X于自身", [{"x": 1, "dodge": False}], "步数不完整"),
    ("发动杀伐X于目标", [{"x": 0, "dodge": False}], "X 非法"),
    ("发动杀伐X于目标", [{"x": 1, "dodge": "no"}], "dodge 非布尔"),
    ("发动不存在X于目标", [{"x": 1}], "非法 AST"),
    ("", [], "空流程"),
])
def test_instant_contract_errors_reject_without_side_effects(flow, steps, why):
    engine = _setup("p2_contract")
    player, foe = engine.state.player, engine.state.enemies[0]
    used0, mana0, hp0 = player.actions_used_this_round, player.current_mana, foe.current_hp
    # 两个 max_iterations 用例走同一入口，只是带上非法的循环上限参数。
    extra = {}
    if "max_iterations 非整数" in why:
        extra["max_iterations"] = "many"
    elif "max_iterations 超安全阀" in why:
        extra["max_iterations"] = 10 ** 9
    resp = _cast(engine, flow, steps, target=foe, **extra)
    assert not resp["success"], why
    assert player.actions_used_this_round == used0
    assert player.current_mana == mana0 and foe.current_hp == hp0


def test_instant_requires_held_daowen():
    engine = _setup("p2_unheld")
    player, foe = engine.state.player, engine.state.enemies[0]
    player.dao_wen.pop("再生")
    resp = _cast(engine, "发动再生X于自身", [{"x": 1}], target=foe)
    assert not resp["success"]


# ---------------------------------------------------------------------------
# Trigger / Lifecycle 数据
# ---------------------------------------------------------------------------

def test_trigger_immediate_parsed_and_old_triggers_unchanged():
    assert parse_trigger("瞬发") == TRIGGER_INSTANT
    assert parse_trigger("immediate") == TRIGGER_INSTANT
    assert TRIGGER_INSTANT in ALL_TRIGGERS
    expected = {
        "受到伤害前": "受到伤害前", "我方受到伤害后": "受到伤害后",
        "失去生命前": "失去生命前", "失去生命后": "失去生命后",
        "目标发动道纹前": "目标发动道纹前",
        "战斗开始时": "战始", "战终": "战终", "回始": "回始", "回终": "回终",
        "敌回始": "敌回始", "敌回终": "敌回终", "自身回合结束": "自身回合结束",
        "闪避时": "闪避时",
    }
    for text, canon in expected.items():
        assert parse_trigger(text) == canon, text


def test_spell_lifecycle_field_defaults_and_roundtrip():
    legacy = Spell(name="旧", required_daowen=["杀伐"], trigger_condition="受到伤害前",
                   effect_flow="发动杀伐X于攻击者")
    assert legacy.lifecycle == LIFECYCLE_PERMANENT  # 旧对象/旧存档 = permanent
    battle = Spell(name="战", required_daowen=["杀伐"], trigger_condition="受到伤害前",
                   effect_flow="发动杀伐X于攻击者", lifecycle=LIFECYCLE_BATTLE)
    d = battle.to_dict()
    assert d["lifecycle"] == LIFECYCLE_BATTLE
    assert {LIFECYCLE_INSTANT, LIFECYCLE_BATTLE, LIFECYCLE_PERMANENT} == {
        "instant", "battle", "permanent"}


def test_define_spell_defaults_battle_and_rejects_instant():
    engine = _setup("p2_define")
    player = engine.state.player
    ok = engine.execute_action("define_spell", {"spell": {
        "name": "反击", "required_daowen": ["杀伐"],
        "trigger_condition": "受到伤害前", "effect_flow": "发动杀伐X于攻击者"}})
    assert ok["success"], ok
    spell = next(s for s in player.spells if s.name == "反击")
    # Part 6：战斗中自创默认 battle 作用域（战终清除）；要跨战斗保留必须显式 permanent。
    assert spell.lifecycle == LIFECYCLE_BATTLE
    assert spell.trigger == "受到伤害前"

    player.actions_used_this_round = 0
    ok2 = engine.execute_action("define_spell", {"spell": {
        "name": "常驻反击", "required_daowen": ["杀伐"],
        "trigger_condition": "受到伤害前", "effect_flow": "发动杀伐X于攻击者",
        "lifecycle": "permanent"}})
    assert ok2["success"], ok2
    assert next(s for s in player.spells if s.name == "常驻反击").lifecycle == LIFECYCLE_PERMANENT

    player.actions_used_this_round = 0
    bad = engine.execute_action("define_spell", {"spell": {
        "name": "瞬斩", "required_daowen": ["杀伐"],
        "trigger_condition": "瞬发", "effect_flow": "发动杀伐X于目标"}})
    assert not bad["success"]
    assert all(s.name != "瞬斩" for s in player.spells)


def test_cast_kind_daowen_unchanged():
    engine = _setup("p2_kind_daowen")
    player, foe = engine.state.player, engine.state.enemies[0]
    used0, hp0 = player.actions_used_this_round, foe.current_hp
    resp = engine.execute_action("cast", {"kind": "daowen", "daowen_name": "杀伐", "x": 1,
                                          "target_ref": _ref(engine, foe), "dodge": False,
                                          "trigger_spell_choices": {}})
    assert resp["success"], resp
    assert foe.current_hp < hp0
    assert player.actions_used_this_round == used0 + 1


# ---------------------------------------------------------------------------
# 2026-09-29 用户裁定：法术本质上就是依次发动多种道纹。
# 瞬发的每一步都与 use_daowen 走同一套"发动道纹"环节：
# 敌方「目标发动道纹前」反应、无神、缄默面具、连击假碎片通道；
# 施法失败（中途中断）出手照扣，不退。
# ---------------------------------------------------------------------------

from engine.models import Relic, StatusEffect

_R_UNUSABLE = REASON_DAOWEN_UNUSABLE


def _give(entity, *names):
    for n in names:
        entity.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="", cost_formula="X", effect_formula=""))


def _zyzq_choices(engine, foe):
    """按 prepare 给出的结构，为持有【咎由自取】的敌人提交 use=True 的反应。"""
    prepared = engine.combat.prepare_daowen_trigger_spells(engine.state.player)
    ref = _ref(engine, foe)
    steps = [{"x": 1, "target_ref": s["target_ref"], "dodge": False}
             for s in prepared[ref][0]["steps"]]
    return {ref: {"咎由自取": {"use": True, "steps": steps}}}


def test_each_instant_step_triggers_target_before_daowen_reactions():
    engine = _setup("p2_zyzq", mana=10)
    player, foe = engine.state.player, engine.state.enemies[0]
    _give(foe, "坠落", "杀伐", "连击")
    foe.spells.append(engine.combat.spell_definition(foe, "咎由自取"))
    foe.mana_limit = foe.current_mana = 20
    hp0 = player.current_hp
    choices = _zyzq_choices(engine, foe)

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 1, "dodge": False, "trigger_spell_choices": choices},
                  {"x": 1, "trigger_spell_choices": choices}], target=foe)

    assert resp["success"], resp
    sr = resp["result"]["step_results"]
    assert [s["status"] for s in sr] == [STEP_COMPLETED, STEP_COMPLETED]
    # 两步各自触发了一次咎由自取（每一步都是一次"发动道纹"）
    for s in sr:
        assert any(log.get("daowen") == "杀伐" and log.get("execution")
                   for log in s["trigger_spell_logs"]), s
    assert foe.current_mana < 20            # 反应方照常付法力
    assert player.current_hp < hp0 + 4      # 被反应打了（再生 4 点也没补回来）


def test_instant_caster_killed_by_reaction_is_normal_interrupt():
    engine = _setup("p2_zyzq_dead", mana=10)
    player, foe = engine.state.player, engine.state.enemies[0]
    _give(foe, "坠落", "杀伐", "连击")
    foe.spells.append(engine.combat.spell_definition(foe, "咎由自取"))
    foe.mana_limit = foe.current_mana = 20
    choices = _zyzq_choices(engine, foe)
    player.current_hp = 1
    used0, foe_hp0 = player.actions_used_this_round, foe.current_hp

    resp = _cast(engine, "发动杀伐X于目标→发动再生X于自身",
                 [{"x": 1, "dodge": False, "trigger_spell_choices": choices},
                  {"x": 1, "trigger_spell_choices": choices}], target=foe)

    assert resp["success"], resp
    assert not player.is_alive
    sr = resp["result"]["step_results"]
    assert [s["status"] for s in sr] == [STEP_INTERRUPTED]   # 第二步没有结果
    assert sr[0]["reason"] == InterruptReason.CASTER_DEAD.value
    assert resp["result"]["execution_status"] == EXECUTION_INTERRUPTED
    assert foe.current_hp == foe_hp0                          # 杀伐没有发出去
    assert player.actions_used_this_round == used0 + 1        # 仅已尝试的第一步收费


def test_instant_missing_trigger_choices_rejected_without_consuming_action():
    engine = _setup("p2_zyzq_missing")
    player, foe = engine.state.player, engine.state.enemies[0]
    _give(foe, "坠落", "杀伐", "连击")
    foe.spells.append(engine.combat.spell_definition(foe, "咎由自取"))
    used0 = player.actions_used_this_round
    resp = _cast(engine, "发动杀伐X于目标", [{"x": 1, "dodge": False}], target=foe)
    assert not resp["success"]
    assert "trigger_spell_choices" in resp["error"]
    assert player.actions_used_this_round == used0


# 2026-10-11 用户令：`test_instant_step_under_wushen_hits_self` 已删除——它整条建立在被删机制上，不是改断言能救的。



def test_instant_silence_mask_interrupts_cost_daowen_step():
    engine = _setup("p2_mask")
    player, foe = engine.state.player, engine.state.enemies[0]
    engine.state.relics.append(Relic("缄默面具", ""))
    hp_foe0 = foe.current_hp
    resp = _cast(engine, "发动杀伐X于目标→发动透支X于自身",
                 [{"x": 1, "dodge": False}, {"x": 1}], target=foe)
    assert resp["success"], resp
    sr = resp["result"]["step_results"]
    assert [s["status"] for s in sr] == [STEP_COMPLETED, STEP_INTERRUPTED]
    assert sr[1]["reason"] == _R_UNUSABLE
    assert foe.current_hp < hp_foe0          # 第一步保留


def test_instant_lianji_fake_shards_insufficient_is_normal_interrupt():
    engine = _setup("p2_lianji_shards")
    player, foe = engine.state.player, engine.state.enemies[0]
    _give(player, "连击")
    engine.state.fake_shards = 0
    engine.state.shards = 0
    used0 = player.actions_used_this_round
    resp = _cast(engine, "发动连击X于目标",
                 [{"x": 1, "cost_channel": "假碎片", "dodge": False}], target=foe)
    assert resp["success"], resp
    assert resp["result"]["step_results"][0]["reason"] == REASON_SHARDS_INSUFFICIENT
    assert resp["result"]["execution_status"] == EXECUTION_INTERRUPTED
    assert player.actions_used_this_round == used0 + 1  # 施法失败出手照扣


def test_instant_lianji_pays_fake_shards_like_use_daowen():
    engine = _setup("p2_lianji_shards_ok")
    player, foe = engine.state.player, engine.state.enemies[0]
    _give(player, "连击")
    engine.state.fake_shards = 60
    resp = _cast(engine, "发动连击X于目标",
                 [{"x": 1, "cost_channel": "假碎片", "dodge": False}], target=foe)
    assert resp["success"], resp
    assert resp["result"]["execution_status"] == EXECUTION_COMPLETED
    assert engine.state.fake_shards == 10
    assert foe.current_hp == foe.blood_limit - 5
