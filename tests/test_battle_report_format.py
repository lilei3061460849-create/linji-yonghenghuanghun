"""
pytest - 战报格式化器（README《六、战斗推演格式》合规性）

对应三项裁定中的第②项：战报必须严格符合 规则正文定义的格式。

覆盖：
- 正常路径：完整一场战斗，产出含全部规范字段的战报
- 边界条件：0个[朋友]/[员工]、无[战始]效果、闪避成功（判定完全失效）
- 错误输入/非法配置：引擎拒绝非法发动时，战报如实记录失败而不伪造数值
"""
import math
import os
import re
import sys
from pathlib import Path

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR


def _new_engine(tmp_path, region="龙心谷"):
    e = GameEngine(db_path=str(tmp_path / "rep.db"), rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((name for name in setup["result"]["relic_choices"] if name not in optional),
                  setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": choice})
    # 本文件不测局外经济，直接构造“精力已耗尽”的合法战始前状态。
    e.state.energy = 0
    return e


def _resolve_monster_phase(e):
    prepared = e.execute_action("prepare_monster_phase", {})
    choices = []
    for actor in prepared["result"]["actors"]:
        attacks = [{"hits": [
            {"target_ref": actor["attack_target_options"][0]["ref"], "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
            for _ in range(actor["base_hits_per_attack"])
        ]} for _ in range(actor["base_attack_actions"])]
        # 2026-09-15 删除白板后：有合法道纹选项时必须提交一个（选首个，且避开会改形态的变形）
        dw = None
        if actor.get("daowen_required"):
            opt = next((o for o in actor["daowen_options"] if o["name"] != "变形"),
                       actor["daowen_options"][0])
            dw = {"name": opt["name"], "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {}}
            if opt.get("requires_target"):
                dw["target_ref"] = actor["attack_target_options"][0]["ref"]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dw,
                        "attack_actions": attacks})
    return e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


# ---------- 开局发现必须写出候选 ----------

def test_setup_discovery_lists_options_then_pick():
    """正常：开局段先列3个候选，再写选择。"""
    lines = BR.format_setup_discovery(
        blood_points=7, speed_points=8, mana_points=10,
        blood_limit=42, mana_limit=20, speed_limit=8, action_count=3, shards=20,
        daowen_options=["波及", "再生", "束缚"], daowen_pick="波及",
        resonance="反转",
        relic_options=["避风铃", "回锋刀", "守夜灯"], relic_pick="避风铃",
        region="罪孽都市",
    )
    text = "\n".join(lines)
    assert text.startswith("[开局]")
    assert "初始道纹发现：候选〔波及、再生、束缚〕→选择【波及】" in text
    assert "遗物发现：候选〔避风铃、回锋刀、守夜灯〕→选择【避风铃】" in text
    assert "副本：罪孽都市" in text


def test_setup_discovery_allows_last_candidate():
    """边界：选择可以是候选最后一项。"""
    lines = BR.format_setup_discovery(
        blood_points=11, speed_points=8, mana_points=6,
        blood_limit=60, mana_limit=14, speed_limit=8, action_count=3, shards=20,
        daowen_options=["杀伐", "庇护", "封印"], daowen_pick="封印",
        resonance="转换",
        relic_options=["避风铃", "守夜灯", "忘忧香"], relic_pick="忘忧香",
    )
    text = "\n".join(lines)
    assert "选择【封印】" in text and "选择【忘忧香】" in text


def test_setup_discovery_rejects_result_only():
    """错误输入：只给选择、不给候选，必须拒绝。"""
    with pytest.raises(ValueError, match="初始道纹发现候选"):
        BR.format_setup_discovery(
            blood_points=11, speed_points=8, mana_points=6,
            blood_limit=60, mana_limit=14, speed_limit=8, action_count=3, shards=20,
            daowen_options=[], daowen_pick="杀伐",
            resonance="反转",
            relic_options=["避风铃", "回锋刀", "守夜灯"], relic_pick="避风铃",
        )
    with pytest.raises(ValueError, match="遗物发现候选"):
        BR.format_setup_discovery(
            blood_points=11, speed_points=8, mana_points=6,
            blood_limit=60, mana_limit=14, speed_limit=8, action_count=3, shards=20,
            daowen_options=["波及", "再生", "束缚"], daowen_pick="波及",
            resonance="反转",
            relic_options=[], relic_pick="避风铃",
        )
    with pytest.raises(ValueError, match="必须落在本次发现候选中"):
        BR.format_setup_discovery(
            blood_points=11, speed_points=8, mana_points=6,
            blood_limit=60, mana_limit=14, speed_limit=8, action_count=3, shards=20,
            daowen_options=["波及", "再生", "束缚"], daowen_pick="杀伐",
            resonance="反转",
            relic_options=["避风铃", "回锋刀", "守夜灯"], relic_pick="避风铃",
        )


def test_setup_linter_rejects_pick_only_report():
    """错误输入：只写选了什么、不写候选的开局段必须被拒。"""
    with pytest.raises(ValueError, match="初始道纹发现候选"):
        BR.validate_setup_discovery_text("[开局]\n初始道纹·杀伐\n遗物·避风铃\n")


# ---------- 正常路径 ----------

def test_battle_start_contains_all_required_fields(tmp_path):
    """正常路径：[战始]段必须逐项含规范要求的5个字段"""
    e = _new_engine(tmp_path)
    bs = e.execute_action("battle_start")
    lines = BR.format_battle_start(
        battle_no=1,
        draw_range="战斗场数1，一阶副本-3最低为1，抽取1只",
        draw_result="、".join(x.name for x in e.state.enemies),
        enemies=e.state.enemies,
        player=e.state.player,
        allies=[],
        background="熔岩隘口",
        start_effects=bs.get("relic_logs", []),
    )
    text = "\n".join(lines)
    for field in ["[战始]", "出怪：", "战斗背景：", "敌方面板：", "我方面板：", "[战始]效果结算："]:
        assert field in text, f"缺少规范字段 {field}"


def test_enemy_panel_matches_spec_shape(tmp_path):
    """正常路径：敌方面板格式 = 名称（攻击次数×攻击力/[血限]，道纹）"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    m = e.state.enemies[0]
    panel = BR.enemy_panel(m)
    assert re.match(rf"^{re.escape(m.name)}（\d+×\d+/\d+，.+）$", panel), panel
    # 数值必须与引擎实体一致，不得杜撰
    assert f"{m.attack_count}×{m.attack_power}/{m.blood_limit}" in panel


def test_monster_hits_listed_one_per_line(tmp_path):
    """正常路径：怪物每一击单独成行，禁止合并结算。
    一轮攻击(attack_count 次)同属一个攻击出手，共用出手号并标注第N/M击，
    但每击仍独立成行（每次攻击独立判定闪避，规则正文）。"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    mp = _resolve_monster_phase(e)
    details = mp["result"]["details"]
    assert details, "resolve_monster_phase 必须上抛逐次出手明细"
    lines = BR.format_monster_hits(1, details)
    assert len(lines) == len(details), "出手行数必须等于实际出手次数"

    seen = []
    for ln, d in zip(lines, details):
        m = re.match(r"^[　]*出手(\d+)（.+?）(?:·第(\d+)/(\d+)击)?：", ln)
        assert m, ln
        seen.append((int(m.group(1)), m.group(2), m.group(3)))
        total = d.get("hit_total") or 1
        if total > 1:
            assert m.group(3) == str(total), f"多击应标注总击数：{ln}"
            assert m.group(2) == str(d.get("hit_index")), f"击序号应与引擎一致：{ln}"

    # 出手号必须从 1 开始且单调不减；同一出手内的多击共用同一个号
    nums = [n for n, _, _ in seen]
    assert nums[0] == 1
    assert all(b - a in (0, 1) for a, b in zip(nums, nums[1:])), nums
    for (n, hi, tot), d in zip(seen, details):
        if tot and int(tot) > 1 and hi != "1":
            assert n == nums[nums.index(n)], "同一出手的后续击必须复用出手号"


def test_multi_hit_round_shares_one_action_number():
    """边界：3 次攻击的一轮攻击只占 1 个出手号，且逐击列出"""
    details = [
        {"attacker": "看门犬", "target": "贾凡", "damage_dealt": 5, "hp_lost": 5,
         "hit_index": i, "hit_total": 3, "new_action": (i == 1)}
        for i in (1, 2, 3)
    ]
    lines = BR.format_monster_hits(4, details)
    assert len(lines) == 3
    assert all("出手4（看门犬）" in ln for ln in lines), lines
    assert "·第1/3击" in lines[0] and "·第3/3击" in lines[2]


def test_single_hit_has_no_hit_marker():
    """边界：单次攻击(attack_count=1)不加"第N/M击"标注"""
    details = [{"attacker": "狙击手", "target": "贾凡", "damage_dealt": 13, "hp_lost": 13,
                "hit_index": 1, "hit_total": 1, "new_action": True}]
    lines = BR.format_monster_hits(2, details)
    assert lines[0].startswith("出手2（狙击手）："), lines[0]
    assert "·第" not in lines[0], "单击不应出现第N/M击标注"


def test_round_end_reports_shield_clear_and_duration(tmp_path):
    """正常路径：[回终]必须写明格挡清空与持续X-1，并给出回合末资源面板"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    e.state.combat_subphase = "await_round_end"  # 本单元仅校验回终格式，跳过怪物行动内容
    re_ = e.execute_action("round_end", {})
    text = "\n".join(BR.format_round_end(re_["result"], e.state.player, e.state.enemies))
    assert "格挡清空" in text
    assert "持续X剩余回合-1" in text
    assert "回合末资源面板" in text


# ---------- 边界条件 ----------

def test_no_allies_and_no_start_effects(tmp_path):
    """边界：无[朋友]/[员工]、无[战始]效果时，字段仍须存在且明确写「无」"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    lines = BR.format_battle_start(
        battle_no=1, draw_range="r", draw_result="x",
        enemies=e.state.enemies, player=e.state.player,
        allies=[], background="废墟据点", start_effects=[],
    )
    text = "\n".join(lines)
    assert "无[朋友]与[员工]" in text
    assert "[战始]效果结算：" in text and "无" in text


def test_dodge_success_is_written_explicitly(tmp_path):
    """边界：闪避成功时必须显式写明消耗1点速度且判定完全失效（铁律5）"""
    detail = [{
        "attacker": "石背熊", "target": "贾凡", "hit_index": 0,
        "dodge_attempted": True, "dodge_success": True,
        "damage_dealt": 0, "shield_absorbed": 0, "hp_lost": 0,
        "target_died": False, "speed_after_dodge": 7,
    }]
    line = BR.format_monster_hits(1, detail)[0]
    assert "消耗1点速度闪避" in line
    assert "判定与结算完全失效" in line
    assert "速度→7" in line


def test_empty_details_yields_no_lines():
    """边界：怪物无出手（空明细）时不产出任何出手行，不得凭空补写"""
    assert BR.format_monster_hits(1, []) == []
    assert BR.format_monster_hits(1, None) == []


# ---------- 错误输入 / 非法配置 ----------

def test_illegal_daowen_is_recorded_as_failure_not_fabricated(tmp_path):
    """错误输入：发动未持有的道纹必须被引擎拒绝，战报不得伪造伤害数值"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    target = e.state.enemies[0].name
    hp_before = e.state.enemies[0].current_hp
    res = e.execute_action("use_daowen", {"daowen_name": "裂变", "x": 1, "target": target})
    assert not res.get("success"), "未持有的道纹必须被拒绝"
    # 目标生命不得发生变化
    assert e.state.enemies[0].current_hp == hp_before
    # 格式化器对失败结果不得产出伤害行
    lines = BR.format_player_action(1, e.state.player.name, res)
    assert not any("原始伤害" in l for l in lines)


def test_formatter_never_invents_numbers_absent_from_engine():
    """非法配置：effects 中出现未知字段时，只做键值透传，不推算数值"""
    fake = {"success": True, "calculation": {}, "execution": {"effects": [{"type": "unknown_x", "foo": 1}]}}
    lines = BR.format_player_action(1, "贾凡", fake)
    text = "\n".join(lines)
    assert "foo=1" in text
    assert "伤害" not in text


# ---------- 端到端：产物合规性 ----------

def test_generated_report_is_spec_compliant(tmp_path):
    """
    正常路径（端到端）：sim/format_trace.py 的产物必须含 README§六 全部规范字段，
    且怪物出手逐条列出（不出现旧版"怪物出手N次"这类概括写法）。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sim"))
    import format_trace

    lines = format_trace.run(region="龙心谷", seed=4, battles=1)
    text = "\n".join(lines)

    for field in ["[开局]", "初始道纹发现：候选", "遗物发现：候选",
                  "[战始]", "出怪：", "战斗背景：", "敌方面板：", "我方面板：",
                  "[战始]效果结算：", "[回始]：", "[回终]："]:
        assert field in text, f"产物缺少规范字段 {field}"
    BR.validate_setup_discovery_text(text)

    # 禁止概括式写法（旧 engine_trace 的违规形态）
    assert not re.search(r"怪物出手\d+次", text), "出现被禁止的概括式结算"

    # 每一次出手都必须单独成行并带行动者
    hits = [l.lstrip("　") for l in lines if l.lstrip("　").startswith("出手")]
    assert hits, "没有任何出手行"
    for h in hits:
        assert re.match(r"^出手\d+（.+?）(?:·第\d+/\d+击)?：", h), h


def test_report_is_reproducible_with_same_seed():
    """边界：同一 seed 必须产出完全一致的战报（可复现，便于核查）"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sim"))
    import format_trace

    a = format_trace.run(region="龙心谷", seed=4, battles=1)
    b = format_trace.run(region="龙心谷", seed=4, battles=1)
    assert a == b, "同一 seed 两次运行结果不一致，战报不可复现"


# ---------- 出手合规性程序校验器（新增三类测试） ----------

def test_action_linter_allows_single_daowen_per_action():
    """边界条件：单回合内每动仅发动1个道纹的合法战报片段必须通过校验"""
    valid_snippet = """## 第1场
第1回合
[回始]：
出手1（莫非）：发动【庇护X=10】→ 消耗10法力
出手2（莫非）：发动【杀伐X=5】→ 消耗5法力
[回终]：
"""
    res = BR.validate_battle_report_actions(valid_snippet)
    assert res["status"] == "compliant"
    assert res["total_actions_validated"] == 2


def test_action_linter_rejects_multi_daowen_bundled_in_single_action():
    """错误输入/非法配置：单次出手内打包发动多个道纹（如超频+爆裂+庇护）必须被校验器拒绝"""
    illegal_snippet = """## 第8场（死斗）
第1回合
[回始]：
出手1（莫非）：
  发动【超频X=3】
  发动【爆裂X=2】
  发动【庇护X=10】
出手2（林渊）：发动【杀伐X=5】
[回终]：
"""
    with pytest.raises(ValueError, match="违规合并发动了多个道纹"):
        BR.validate_battle_report_actions(illegal_snippet)


def test_action_linter_allows_instant_spell_steps_in_one_action():
    """边界：一次【瞬发法术】是一条决策，多步在同一出手内依次发动不算合并打包。"""
    snippet = """## 第1场
第1回合
[回始]：
出手1（莫非）：[动作声明] 瞬发法术：发动【杀伐X=3】→发动【再生X=2】
[回终]：
"""
    res = BR.validate_battle_report_actions(snippet)
    assert res["status"] == "compliant"
    assert res["total_actions_validated"] == 1


def test_action_linter_allows_consecutive_actions_when_opponent_budget_exhausted():
    """边界条件：当对手出手预算耗尽时，出手多的一方连续执行剩余出手（正文铁律：一方出手耗尽后另一方余下出手继续）必须合法通过"""
    asymmetric_snippet = """## 第8场（死斗）
守擂冠军：林渊（42/50/6，出手2次）
挑战胜者：莫非（42/52/12，出手4次）
第1回合
[回始]：
出手1（莫非·第1动）：[动作声明] 发动【退化X=2】
出手2（林渊·第1动）：[动作声明] 发动【加害X=2】
出手3（莫非·第2动）：[动作声明] 发动【超频X=3】
出手4（林渊·第2动）：[动作声明] 发动【庇护X=12】
出手5（莫非·第3动）：[动作声明] 发动【爆裂X=2】
出手6（莫非·第4动）：[动作声明] 发动【庇护X=10】
[回终]：
"""
    res = BR.validate_battle_report_actions(asymmetric_snippet)
    assert res["status"] == "compliant"
    assert res["total_actions_validated"] == 6


def test_action_linter_rejects_exceeding_action_budget():
    """错误输入/非法配置：单回合内出手次数超过 action_count 必须被校验器拒绝"""
    exceed_budget_snippet = """## 第8场（死斗）
守擂冠军：林渊（42/50/6，出手2次）
挑战胜者：莫非（42/52/12，出手4次）
第1回合
[回始]：
出手1（莫非·第1动）：[动作声明] 发动【退化X=2】
出手2（林渊·第1动）：[动作声明] 发动【加害X=2】
出手3（林渊·第2动）：[动作声明] 发动【庇护X=12】
出手4（林渊·第3动）：[动作声明] 发动【杀伐X=5】
[回终]：
"""
    with pytest.raises(ValueError, match="超过 action_count 上限"):
        BR.validate_battle_report_actions(exceed_budget_snippet)




def test_round_start_renders_existing_mechanism_string_without_inventing_effects():
    from engine.battle_report import _render_effect
    assert _render_effect("守夜灯：获得10法力")=="守夜灯：获得10法力"
