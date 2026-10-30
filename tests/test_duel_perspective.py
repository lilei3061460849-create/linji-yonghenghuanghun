"""
死斗【当事人视角】前台战报测试。

对应任务书第12条要求，覆盖：
1. A无法从前台战报看到B的HP/法力/速度。
2. A无法看到B的道纹/遗物/残韵。
3. B同样无法看到A的隐藏资源。
4. 角色实际说的话能够正常进入前台战报。
5. 角色说谎不会被战报旁白揭穿。
6. 行动结果能够正常展示。
7. 未公开的具体道纹不会因为后台知道而泄露到前台。
8. 心理活动不会进入前台。
9. 性格数据不会进入前台。
10. 后台审计数据仍然完整。
11. 现有非死斗战报不受影响。
12. save/load、事务回滚、死斗结算全部正常（复用现有死斗测试路径，只追加
    perspective 断言，不改动结算流程）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.setup_support import finish_initial_daowen
from tests.attack_support import resolve_attack as do_attack

from engine.api import GameEngine
from engine.enums import GamePhase
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect
from engine import battle_report as BR
from engine import duel_perspective as DP


def _duel_engine(tmp_path, *, lord_relics=("守夜灯",), lord_hp=40, lord_mana=30, lord_speed=12,
                  lord_daowen=("杀伐", "庇护"), challenger_hp=40, challenger_speed=9):
    """与 tests/test_duel_pvp_guards.py 同款最小死斗夹具：直接构造 in_final_duel 状态，
    不依赖完整的7场通关流程，专注验证信息隔离。"""
    e = GameEngine(db_path=str(tmp_path / "d.db"), rng_seed=1,
                   sealed_candidate_path=str(tmp_path / "s.json"),
                   death_book_path=str(tmp_path / "b.md"))
    e.execute_action("setup_attributes", {
        "name": "挑战者甲", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    p = e.state.player
    p.current_hp = challenger_hp
    p.blood_limit = challenger_hp
    p.current_speed = challenger_speed
    p.speed_limit = challenger_speed
    p.current_mana = 20
    p.mana_limit = 20
    p.dao_wen["杀伐"] = DaoWenInstance(DaoWen(
        name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    p.dao_wen["庇护"] = DaoWenInstance(DaoWen(
        name="庇护", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    p.status_effects.append(StatusEffect(name="加害", remaining_rounds=-1, value=2, source="挑战者甲"))
    e.state.relics = [Relic(name="无所求", effect="", tags=[])]

    lord = Entity("守擂乙", "轮回者", blood_limit=lord_hp, current_hp=lord_hp,
                  mana_limit=lord_mana, current_mana=lord_mana,
                  speed_limit=lord_speed, current_speed=lord_speed)
    for n in lord_daowen:
        lord.dao_wen[n] = DaoWenInstance(DaoWen(
            name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    e.state.enemies.clear()
    e.state.enemies.append(lord)
    e.state.opponent_relics = [Relic(name=n, effect="", tags=[]) for n in lord_relics]
    e.state.in_final_duel = True
    e.state.duel_turn = "player_side"
    e.state.phase = GamePhase.IN_COMBAT.value
    e.state.combat_subphase = "await_round_start"
    return e


# ---------------------------------------------------------------------------
# 1-3. 隐藏资源隔离：真实死斗结算 + 生成前台战报，逐项断言数值/名称不泄露。
# ---------------------------------------------------------------------------

def test_perspective_report_hides_opponent_hp_mana_speed(tmp_path):
    """1&3：前台战报文本中不得出现任一方的真实HP/血限/法力/法限/速度数值面板。"""
    e = _duel_engine(tmp_path)
    r = e.execute_action("round_start", {"relic_choices": {}})
    assert r["success"]

    entries = []
    DP.record_environment(entries, e.state.current_round, "死斗擂台，龙心断罪深渊。")
    DP.record_speech(entries, e.state.current_round, "挑战者甲", "我准备治疗。")

    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert res["success"]
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )

    lines = DP.render_report(entries)
    text = "\n".join(lines)

    # 真实血限/法限/速限数值一律不得出现（40/30/12/20/9 等）
    for forbidden_number in (str(e.state.enemies[0].blood_limit),
                             str(e.state.enemies[0].mana_limit),
                             str(e.state.enemies[0].speed_limit),
                             str(e.state.enemies[0].current_mana),
                             str(e.state.player.mana_limit),
                             str(e.state.player.speed_limit)):
        assert forbidden_number not in text, f"前台战报泄露了隐藏数值 {forbidden_number}：\n{text}"
    assert "血限" not in text and "法限" not in text and "速限" not in text
    assert "法力" not in text and "速度" not in text


def test_perspective_report_hides_daowen_relics_resonance(tmp_path):
    """2：前台战报不得出现任一方未被公开揭示的道纹/遗物/残韵具体名称。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    entries = []
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert res["success"]
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )
    lines = DP.render_report(entries)
    text = "\n".join(lines)
    assert "杀伐" not in text, "未显式 reveal 时，具体道纹名不得出现在前台"
    assert "守夜灯" not in text and "无所求" not in text, "遗物名不得出现在前台"
    assert "残韵" not in text


def test_opponent_side_also_cannot_see_challenger_hidden_state(tmp_path):
    """3：对称验证——守擂方（对手）视角同样看不到挑战者的隐藏资源。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    e.state.duel_turn = "opponent_side"
    res = e.execute_action("use_daowen", {
        "actor_ref": "enemy:0", "daowen_name": "杀伐", "x": 3, "target_ref": "player:0"})
    assert res["success"]
    entries = []
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="opponent_side", actor_name="守擂乙", result=res,
        target_side="player_side", target_name="挑战者甲",
    )
    text = "\n".join(DP.render_report(entries))
    assert str(e.state.player.current_hp) not in text or e.state.player.current_hp == 0
    assert "加害" not in text  # 挑战者身上的 buff 不得泄露给对手视角的前台文本
    assert "杀伐" not in text


# ---------------------------------------------------------------------------
# 4-5. 角色说话：进入战报但不被验真/揭穿。
# ---------------------------------------------------------------------------

def test_speech_enters_report_and_is_not_fact_checked(tmp_path):
    """4&5：A声称"准备治疗"但实际发动伤害道纹，前台不得替角色澄清或拆穿。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    entries = []
    DP.record_speech(entries, e.state.current_round, "挑战者甲", "我准备治疗。")
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert res["success"]
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )
    lines = DP.render_report(entries)
    text = "\n".join(lines)
    assert "挑战者甲：「我准备治疗。」" in text
    # 不允许出现"欺骗""说谎""其实是""真实目的"等揭穿式旁白
    for tell in ("欺骗", "说谎", "其实是", "真实目的", "谎言", "骗过"):
        assert tell not in text
    # 但公开可观察的结果（直接失去生命）必须仍然存在，且不得误写成伤害。
    assert "守擂乙 失去25点生命" in text
    assert "点伤害" not in text


def test_lie_matching_readme_example_exact_shape(tmp_path):
    """5：完全复刻任务书给出的示例——A声明治疗、实际杀伐，前台只写公开结果。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    entries = []
    DP.record_speech(entries, e.state.current_round, "A", "我准备治疗。")
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 10, "target_ref": "enemy:0"})
    assert res["success"]
    lost = next(ef["lost"] for ef in res["execution"]["effects"] if ef["type"] == "target_life_loss")
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="A", result=res,
        target_side="opponent_side", target_name="B(守擂乙)",
    )
    # 手动重命名 target 以贴合示例中的“B”称呼（数据本身来自真实引擎结果，不编造）
    for entry in entries:
        if entry.get("kind") == "action":
            for fact in entry["facts"]:
                if fact.get("target"):
                    fact["target"] = "B"
    text = "\n".join(DP.render_report(entries))
    assert "A：「我准备治疗。」" in text
    assert "A欺骗了B" not in text
    assert "杀伐" not in text
    assert f"B 失去{lost}点生命" in text


# ---------------------------------------------------------------------------
# 6. 行动结果正常展示（伤害/闪避/命零/撤退）。
# ---------------------------------------------------------------------------

def test_dodge_and_damage_results_are_shown(tmp_path):
    """6：闪避与伤害必须作为公开可观察结果正常出现。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_power/count 无效。
    # 夹具给了 速度9/法力20 → 一手 9击×20 = 180 伤，会把 40 血的守擂乙直接打死，
    # 后面那条"守擂还手"就找不到存活行动者了。这里压到 1击×5伤，让它活着。
    e.state.player.current_speed = 1
    e.state.player.current_mana = 5
    r1 = do_attack(e, "挑战者甲", [], dodge=False)
    assert r1["success"], r1
    entries = []
    DP.record_attack(entries, e.state.current_round,
                     actor_side="player_side", actor_name="挑战者甲",
                     target_side="opponent_side", hits=r1["result"]["hits"])
    text = "\n".join(DP.render_report(entries))
    assert "守擂乙 受到5点伤害" in text   # 攻力=当前法力=5

    e.state.duel_turn = "opponent_side"
    # 轮回者攻力=当前法力、攻次=当前速度，写面板无效
    e.state.enemies[0].current_mana = 6
    e.state.enemies[0].current_speed = 1
    r2 = do_attack(e, "守擂乙", [], dodge=True)
    assert r2["success"], r2
    entries2 = []
    DP.record_attack(entries2, e.state.current_round,
                     actor_side="opponent_side", actor_name="守擂乙",
                     target_side="player_side", hits=r2["result"]["hits"])
    text2 = "\n".join(DP.render_report(entries2))
    assert "挑战者甲 选择闪避，判定完全失效" in text2


def test_death_and_retreat_are_observable(tmp_path):
    """6：命零/撤退这类公开可观察结果必须能展示。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    e.state.enemies[0].current_hp = 3
    e.state.player.attack_power = 10
    e.state.player.attack_count = 1
    r = do_attack(e, "挑战者甲", [], dodge=False)
    assert r["success"]
    entries = []
    DP.record_attack(entries, e.state.current_round,
                     actor_side="player_side", actor_name="挑战者甲",
                     target_side="opponent_side", hits=r["result"]["hits"])
    text = "\n".join(DP.render_report(entries))
    assert "命零" in text


# ---------------------------------------------------------------------------
# 7. 未公开的具体道纹不会因为后台知道而泄露到前台（含 reveal_name 显式授权路径）。
# ---------------------------------------------------------------------------

def test_undisclosed_daowen_name_never_leaks_by_default(tmp_path):
    """7：即便后台完整知道发动的是【杀伐】，默认也不写入前台。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert res["success"]
    assert res["calculation"]["dao_wen"] == "杀伐", "后台数据必须真实含有道纹名（审计不受影响）"
    entries = []
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )
    text = "\n".join(DP.render_report(entries))
    assert "杀伐" not in text
    assert "一种未公开的能力" in text


def test_reveal_name_requires_explicit_opt_in(tmp_path):
    """7（边界）：只有调用方显式传入 reveal_name，前台才会写出具体名称。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    entries = []
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
        reveal_name="杀伐",
    )
    text = "\n".join(DP.render_report(entries))
    assert "杀伐" in text


# ---------------------------------------------------------------------------
# 8. 心理活动不会进入前台。
# ---------------------------------------------------------------------------

def test_no_internal_thoughts_in_report(tmp_path):
    """8：即便驱动脚本手滑往备注里塞心理活动词汇，也不能通过合法 API 写入。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    entries = []
    DP.record_speech(entries, e.state.current_round, "挑战者甲", "我准备治疗。")
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )
    text = "\n".join(DP.render_report(entries))
    for phrase in ("心想", "他判断", "他准备发动", "认为对方", "猜测", "识破", "看穿"):
        assert phrase not in text, f"前台混入心理活动措辞：{phrase}"


# ---------------------------------------------------------------------------
# 9. 性格数据不会进入前台。
# ---------------------------------------------------------------------------

def test_personality_never_appears_in_report(tmp_path):
    """9：即使角色已经积累了性格证据，前台文本里也不能出现性格标签/维度名。"""
    e = _duel_engine(tmp_path)
    for i in range(5):
        e.update_personality(e.state.player, "risk_preference", 1.0, evidence=f"证据{i}")
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    entries = []
    DP.record_daowen(
        entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )
    text = "\n".join(DP.render_report(entries))
    for tag in ("风险偏好", "冒险", "求稳", "人格", "性格", "risk_preference"):
        assert tag not in text


# ---------------------------------------------------------------------------
# 10. 后台审计数据仍然完整；前后台拼接后两段都在、且互不吞没。
# ---------------------------------------------------------------------------

def test_audit_section_still_complete_after_assembly(tmp_path):
    """10：assemble_duel_report 拼接后，【后台审计数据】段落必须完整保留现有格式字段。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert res["success"]

    perspective_entries = []
    DP.record_speech(perspective_entries, e.state.current_round, "挑战者甲", "我准备治疗。")
    DP.record_daowen(
        perspective_entries, e.state.current_round,
        actor_side="player_side", actor_name="挑战者甲", result=res,
        target_side="opponent_side", target_name="守擂乙",
    )
    perspective_lines = DP.render_report(perspective_entries)

    audit_lines = []
    audit_lines.extend(BR.format_player_action(1, "挑战者甲", res))

    full = DP.assemble_duel_report(perspective_lines, audit_lines)
    text = "\n".join(full)

    assert "【当事人视角】" in text
    assert "【后台审计数据】" in text
    # 后台段必须含真实道纹名与完整数值——审计数据不受信息隐藏影响
    assert "杀伐" in text
    assert "target_life_loss" in text and "lost=25" in text
    # 前台段（分隔符之前）不得含道纹名
    split_idx = text.index("【后台审计数据】")
    front = text[:split_idx]
    back = text[split_idx:]
    assert "杀伐" not in front
    assert "杀伐" in back


def test_audit_uses_existing_battle_report_functions_unmodified(tmp_path):
    """10（回归护栏）：本次改动不得修改 engine/battle_report.py 的既有输出——
    用完全相同的输入调用 format_player_action，输出必须与改动前的规范完全一致
    （通过复用既有测试断言的字段来做最小锚点校验，防止未来有人为了前台改后台）。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    lines = BR.format_player_action(1, "挑战者甲", res)
    text = "\n".join(lines)
    assert "出手1（挑战者甲）：" in text
    assert "发动【杀伐X=5】" in text
    assert "target_life_loss" in text and "lost=25" in text


# ---------------------------------------------------------------------------
# 11. 现有非死斗战报不受影响（PvE 战报格式化器零回归）。
# ---------------------------------------------------------------------------

def test_non_duel_battle_report_format_untouched(tmp_path):
    """11：普通 PvE 战斗（非死斗）的战报格式化路径完全不受本次改动影响。"""
    e = GameEngine(db_path=str(tmp_path / "pve.db"), rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": choice})
    e.state.energy = 0
    bs = e.execute_action("battle_start")
    assert bs["success"]
    lines = BR.format_battle_start(
        battle_no=1, draw_range="战斗场数1，一阶副本-3最低为1，抽取1只",
        draw_result="、".join(x.name for x in e.state.enemies),
        enemies=e.state.enemies, player=e.state.player, allies=[],
        background="熔岩隘口", start_effects=bs.get("relic_logs", []),
    )
    text = "\n".join(lines)
    for field in ["[战始]", "出怪：", "战斗背景：", "敌方面板：", "我方面板：", "[战始]效果结算："]:
        assert field in text
    # 敌方面板里包含真实血限/攻击信息——非死斗场景不受“信息隐藏”约束
    m = e.state.enemies[0]
    assert f"{m.attack_count}×{m.attack_power}/{m.blood_limit}" in text


# ---------------------------------------------------------------------------
# 12. save/load、事务回滚、死斗结算全部正常。
# ---------------------------------------------------------------------------

def test_saveload_unaffected_by_perspective_module(tmp_path):
    """12：本模块是纯函数、不持有引擎引用，save/load 生命周期完全不受影响。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert res["success"]
    entries = []
    DP.record_daowen(entries, e.state.current_round, actor_side="player_side",
                     actor_name="挑战者甲", result=res, target_side="opponent_side",
                     target_name="守擂乙")
    DP.render_report(entries)  # 调用本模块不应对引擎产生任何副作用

    save_res = e.save_game("perspective_test")
    assert save_res["success"]
    hp_before = e.state.player.current_hp
    e2 = GameEngine(db_path=str(tmp_path / "d.db"), rng_seed=1,
                    sealed_candidate_path=str(tmp_path / "s.json"),
                    death_book_path=str(tmp_path / "b.md"))
    load_res = e2.load_game("perspective_test")
    assert load_res["success"]
    assert e2.state.player.current_hp == hp_before


def test_transaction_rollback_unaffected(tmp_path):
    """12：非法行动仍然整体回滚，本模块不参与结算，不改变回滚行为。"""
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    hp_before = e.state.enemies[0].current_hp
    mana_before = e.state.player.current_mana
    bad = e.execute_action("use_daowen", {"daowen_name": "不存在的道纹", "x": 1, "target_ref": "enemy:0"})
    assert bad["success"] is False
    assert e.state.enemies[0].current_hp == hp_before
    assert e.state.player.current_mana == mana_before


def test_duel_resolution_flow_unaffected(tmp_path):
    """12：死斗结算（resolve_final_duel）路径完全不受前台模块影响。"""
    e = _duel_engine(tmp_path)
    e.state.enemies[0].current_hp = 0
    e.state.enemies[0].is_alive = False
    r = e.execute_action("resolve_final_duel", {"outcome": "victory"})
    assert r["success"]
    assert r["result"]["outcome"] == "victory"
