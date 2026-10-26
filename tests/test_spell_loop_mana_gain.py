"""循环法术：执行器逐轮迭代，每轮重读真实状态；调用方不再提交 cycles。

架构（2026-10-02，Part 4）：DSL 里的【循环】由 SpellExecution 的 LoopStep 拥有。
调用方为程序里每个 ActionStep 提交一条决策（X/目标/闪避），可选地给一个
``max_iterations`` 上限（"我最多跑几轮"），但**不能**预先展开每轮的步骤列表。
终止条件全部由规则给出：法力耗尽、施法者命零、目标失效、本轮无进展、
DSL 定次（循环N次）。10000 次是工程安全阀，不是游戏规则。

本文件锁定三条真实语义：
1. 零法力起步的【透支】+【再生】自持循环可以提交并结算（校验期不得把
   产法力道纹当成纯支出）；
2. 每轮法力净零、生命净零，累计回复推进【癌变】阈值 → 到顶即命零、循环终止；
3. 付不起的循环仍然会被拒绝——只是从"提交时报错"变成"执行期中断"。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import StatusEffect,  DaoWen, DaoWenInstance, Relic
from tests.setup_support import grant_extra_actions
from tests.test_dragon_heart import _new_engine, _start_with_enemy

# 与官方条目 死者之书.md「## 可学法术 → 血炼周天」同构（现已是内置法术，
# 这里刻意用另一个名字自创同一流程，以覆盖"自创循环法术"这条路径）。先【再生】回血、再
# 【透支】把血卖成法力，【透支】的流血同时满足「失去生命后」驱动下一轮。
SPELL = {
    "name": "周天自持",
    "required_daowen": ["再生", "透支"],
    "trigger_condition": "失去生命后",
    "effect_flow": "发动再生X于自身→发动透支X于自身→循环",
}
SEED_MANA = 3


def _engine_with_loop_spell(suffix, *, hp, mana, spells=None):
    engine = _new_engine(suffix)
    player = engine.state.player
    engine.state.relics = []   # 2026-10-13：遗物池删2件后随机发放漂移（万钧印改向攻击会打断循环触发），本文件只测法术循环，清空钉死。
    for name in ("透支", "再生"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    # 2026-09-16：自创法术只能在战斗中用 define_spell 完成。
    _start_with_enemy(engine)
    # 2026-10-25：法术阶数＝重数。周天自持 = 失去生命后 + 再生 + 透支 = 3 阶 = 3 次出手；
    # 条件分支的两臂各算一重，本文件的「血溅」分支写法到 6 阶。基础出手只 2 次，
    # 先用【蓄锐·增】把预算补到 11（9 阶上限 + 基础 2）再定义。
    grant_extra_actions(engine.state.player, 9)  # 战斗开始时可能替换 player 对象，取最新引用
    _define_spells(engine, spells if spells is not None else (SPELL,))
    foe = engine.state.enemies[0]
    foe.current_hp = 99999
    foe.attack_power = 5
    foe.attack_count = 1
    player.current_hp = hp
    player.current_mana = mana
    return engine


def _define_spells(engine, spells):
    """战斗中自创法术（2026-09-16 新规则入口；按阶级消耗主动出手）。"""
    for spell in spells:
        result = engine.execute_action("define_spell", {"spell": spell})
        assert result["success"], result.get("error")
        assert result["result"]["wired"] is True


def _fire(engine, max_iterations, x=3):
    """让怪物普攻一次触发【失去生命后】，提交 max_iterations 轮上限。"""
    combat = engine.combat
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    options = target["spell_options"]
    assert any(s["spell_name"] == "周天自持" and s["loop"] for s in options["after"])
    schema = next(s for s in options["after"] if s["spell_name"] == "周天自持")
    # 每个决策槽位一条；循环体在每个决策槽位上各列一次（不是每轮一条）。
    assert len(schema["steps"]) == 2

    steps = [{"x": x, "target_ref": "player:0"} for _ in schema["steps"]]
    spell_choices = {
        "before": {s["spell_name"]: {"use": False} for s in options["before"]},
        "after": {"周天自持": {"use": True, "steps": steps,
                              "max_iterations": max_iterations}},
        "damage_after": {s["spell_name"]: {"use": False} for s in options["damage_after"]},
        "life_before": {s["spell_name"]: {"use": False} for s in options["life_before"]},
    }
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [{"hits": [{"target_ref": target["ref"], "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": spell_choices}]}],
    }]
    return combat.resolve_monster_phase(choices, prepared=prepared)


def _spell_logs(result, name="周天自持"):
    return [log for detail in result for log in detail.get("spell_logs", [])
            if log.get("spell") == name]


BRANCH_SPELL = {
    "name": "血溅五步",
    "required_daowen": ["再生", "透支", "杀伐"],
    "trigger_condition": "失去生命后",
    "effect_flow": (
        "若自身 法力 大于等于 2 则 "
        "发动再生X于自身；发动杀伐X于攻击者；发动透支X于自身 "
        "否则 发动再生X于自身；发动透支X于自身→循环"
    ),
}


def _engine_with_branch_spell(suffix, *, mana):
    """用真实 GameEngine 定义【血溅五步】并把怪物设为三击靶场。"""
    engine = _new_engine(suffix)
    player = engine.state.player
    engine.state.relics = []   # 同上（承露盏由本夹具显式追加，不受影响）。
    # 2026-10-25 用户令：法术阶级＝重数。血溅五步 = 失去生命后 + 条件分支两臂各 2 步
    # = 6 阶，须一次付清 6 次出手；基础预算只有 2 次，故用【蓄锐·增】把预算补足。
    # （action_count 是只读属性，只能走正文的修正项。）
    grant_extra_actions(engine.state.player, 9)
    for name in ("杀伐", "再生", "透支"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))

    # 让前一击的真实失血与【承露盏】在同一阶段内把法力从<2推到≥2，
    # 直接覆盖用户要求的“承露盏+血溅五步”引擎路径。
    engine.state.relics.append(Relic(
        name="承露盏", effect="每累计失去10点生命，获得1点法力"))
    _start_with_enemy(engine)
    _define_spells(engine, (BRANCH_SPELL,))
    foe = engine.state.enemies[0]
    foe.current_hp = 99999
    foe.attack_power = 6
    foe.attack_count = 3
    engine.state.player.current_hp = 40
    engine.state.player.current_mana = mana
    return engine


def _branch_steps(x=1):
    """程序有 5 个决策槽位：则分支 3 个（再生/杀伐/透支）→ 否则分支 2 个（再生/透支）。"""
    return [
        {"x": x, "target_ref": "player:0"},
        {"x": x, "target_ref": "enemy:0", "dodge": False},
        {"x": x, "target_ref": "player:0"},
        {"x": x, "target_ref": "player:0"},
        {"x": x, "target_ref": "player:0"},
    ]


def _branch_choices(option, *, use, decision=None, max_iterations=1):
    """按 prepare 返回的真实资格集生成一份完整反应提交（两个分支的槽位都覆盖）。"""
    spell_choices = {
        timing: {
            s["spell_name"]: {"use": False}
            for s in option["spell_options"].get(timing, [])
        }
        for timing in ("before", "after", "damage_after", "life_before")
    }
    if use:
        spell_choices["after"]["血溅五步"] = decision or {
            "use": True, "steps": _branch_steps(), "max_iterations": max_iterations}
    return spell_choices


def _resolve_branch_phase(engine, hits):
    """通过 CombatEngine 的 prepare/resolve 合约结算一次真实怪物阶段。

    hits: [(use, spell_choices), ...]，每次命中一份提交；把同一份 dict 传给
    多个命中，就是"同一份提交在多次命中中复用"（条件分支冻结的真实场景）。
    """
    prepared = engine.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    option = target
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [{"hits": [
            {"target_ref": target["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": _branch_choices(option, use=use, decision=decision)}
            for use, decision in hits
        ]}],
    }]
    return engine.combat.resolve_monster_phase(choices, prepared)


def test_blood_splash_branch_evaluated_at_execution_and_frozen_across_hits():
    """条件分支在执行到时求值；同一份提交在后续命中里复用首次的分支选择。"""
    engine = _engine_with_branch_spell("branch_low", mana=1)
    decision = {"use": True, "steps": _branch_steps(), "max_iterations": 1}
    # 第一个命中到达条件时法力=1 → 走"否则"分支（2 步）；
    # 结算后【透支】把法力推到 2，但第二、三次命中复用同一份提交 → 分支冻结。
    result = _resolve_branch_phase(engine, [
        (True, decision),
        (True, decision),
        (False, None),
    ])
    assert result and engine.state.player.is_alive
    assert engine.state.player.current_mana == 3
    used = [log["daowen"] for log in _spell_logs(result, "血溅五步") if "daowen" in log]
    assert used == ["再生", "透支", "再生", "透支"]
    # 冻结快照随提交写回调用方，且归属标记可序列化（字符串）
    assert decision["branch_snapshot"] == {"0L0": 1}
    assert isinstance(decision["_engine_branch_owner"], str)
    assert decision["branch_snapshot"]


def test_blood_splash_high_mana_branch_submits_three_steps_and_loops():
    """高法力分支按当前状态求值：再生→杀伐→透支（各一步）。"""
    engine = _engine_with_branch_spell("branch_high", mana=2)
    result = _resolve_branch_phase(engine, [
        (True, {"use": True, "steps": _branch_steps(), "max_iterations": 1}),
        (False, None),
        (False, None),
    ])
    assert result and engine.state.player.is_alive
    used = [log["daowen"] for log in _spell_logs(result, "血溅五步") if "daowen" in log]
    assert used == ["再生", "杀伐", "透支"]


def test_single_iteration_is_mana_neutral():
    """一轮循环法力净零：再生耗3、透支产3，法力回到起点，生命净+3。"""
    engine = _engine_with_loop_spell("loop_one", hp=40, mana=SEED_MANA)
    player = engine.state.player

    _fire(engine, max_iterations=1)

    # 挨打5点后触发：再生3(耗3法力、回12生命) → 透支3(流血12、产3法力)
    assert player.current_hp == 35          # 40-5+12-12
    assert player.current_mana == SEED_MANA  # 用3产3，回到起点
    assert player.total_healed == 12


def test_loop_without_seed_mana_interrupts_as_mana_insufficient():
    """首步是【再生】，零法力起不来：执行期中断（正常游戏结果，不是异常）。"""
    engine = _engine_with_loop_spell("loop_noseed", hp=40, mana=0)
    player = engine.state.player

    result = _fire(engine, max_iterations=1)

    logs = _spell_logs(result)
    assert logs and logs[0]["interrupted"] == "mana_insufficient"
    assert player.current_hp == 35          # 只有挨打那 5 点
    assert player.current_mana == 0
    assert player.is_alive


def test_loop_is_mana_neutral_across_many_iterations():
    """多轮循环同样零法力自持，每轮净 0 生命（4:1 对称闭环）。"""
    engine = _engine_with_loop_spell("loop_many", hp=40, mana=SEED_MANA)
    player = engine.state.player

    _fire(engine, max_iterations=10)

    assert player.current_mana == SEED_MANA  # 10轮之后法力仍回到起点
    assert player.total_healed == 120        # 10轮 × 再生3回12
    assert player.current_hp == 35           # 40-5 + 10×(12-12)：闭环净零
    assert player.is_alive


def test_cancer_still_caps_the_loop():
    """癌变是天然闸门：累计回复达2×血限即命零，max_iterations 再大也止步于此。"""
    for max_iterations in (11, 50):
        engine = _engine_with_loop_spell(f"loop_cap_{max_iterations}", hp=40, mana=SEED_MANA)
        player = engine.state.player
        cap = 2 * player.blood_limit

        result = _fire(engine, max_iterations=max_iterations)

        assert player.total_healed == cap   # 132，不因提交更多轮数而继续累加
        assert player.current_hp == 0
        assert not player.is_alive
        # 第 11 轮的【透支】不再结算：循环因施法者命零而中止
        logs = _spell_logs(result)
        assert logs[-1]["interrupted"] == "caster_dead"
        assert max_iterations == 11 or len(logs) == 22


def test_unaffordable_pure_cost_loop_interrupts_without_crashing():
    """没有产法力步骤的循环：法力不足时执行期中断，不吞成异常、不半途写状态。"""
    spell = {
        "name": "纯耗周天",
        "required_daowen": ["再生"],
        "trigger_condition": "失去生命后",
        "effect_flow": "发动再生X于自身→循环",
    }
    engine = _engine_with_loop_spell("loop_broke", hp=40, mana=0, spells=(spell,))
    player = engine.state.player

    combat = engine.combat
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    options = target["spell_options"]
    after = {s["spell_name"]: {"use": False} for s in options["after"]}
    assert "纯耗周天" in after
    after["纯耗周天"] = {"use": True,
                        "steps": [{"x": 3, "target_ref": "player:0"}],
                        "max_iterations": 5}
    spell_choices = {
        "before": {s["spell_name"]: {"use": False} for s in options["before"]},
        "after": after,
        "damage_after": {s["spell_name"]: {"use": False} for s in options["damage_after"]},
        "life_before": {s["spell_name"]: {"use": False} for s in options["life_before"]},
    }
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [{"hits": [{"target_ref": target["ref"], "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": spell_choices}]}],
    }]
    result = combat.resolve_monster_phase(choices, prepared=prepared)
    logs = [log for detail in result for log in detail.get("spell_logs", [])
            if log.get("spell") == "纯耗周天"]
    assert logs and logs[0]["interrupted"] == "mana_insufficient"
    assert player.current_mana == 0
    assert player.is_alive
