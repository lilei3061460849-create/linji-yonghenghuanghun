"""普攻作为常驻 AI 候选（DM裁定 2026-09-09，实验旗标 LJ_AI_BASIC_ATTACK=1）。

裁定背景：法力改一池制后不再每回合回填，只靠法力型道纹会在池子花干后无事可做。
实测同批种子（0-99，扭曲都市 7 场）：
  · 一池制、AI 无普攻：通关 0/100，平均经历 0.87 场
  · 一池制、AI 有普攻：通关 0/100，平均经历 1.02 场（1×1 普攻只打 1 点，救不动）
  · 再加攻次/攻力加点 + 修行买攻：普攻武斗通关 1/100，平均经历 4.26 场

普攻是两段动作（prepare_attack → resolve_attack，resolve 需要 prepare 给的 token），
所以预演器扩成能吃动作序列，AI 才能看见普攻的真实伤害并参与打分。

2026-10-02：原 `test_at_one_by_one_daowen_still_wins`（1×1 面板下杀伐仍应优先）
在攻次/攻力改制后已失效——它只锁实验规则 AI 的候选排序，与正式 LLM 路径无关，
现以 xfail 形式移入 tests/test_legacy_rule_ai_contracts.py 留档。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from engine.ai_tactics import TacticalAI  # noqa: E402
from engine.api import GameEngine  # noqa: E402
from tests.setup_support import (  # noqa: E402
    begin_battle, begin_round, finish_initial_daowen, strip_monster_relics,
)

FLAG = "LJ_AI_BASIC_ATTACK"


@pytest.fixture
def engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "ai_ba.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "ai_ba.json"))
    # DM裁定 2026-09-10：攻次=当前速度、攻力=当前法力；加点只剩血/速/法三维（2点一档）
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 1,
                                          "speed_points": 8, "mana_points": 16})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    begin_battle(e)
    begin_round(e)
    return e


def test_flag_zero_disables_basic_attack_candidate(engine, monkeypatch):
    """错误输入/对照：LJ_AI_BASIC_ATTACK=0 显式关闸时与旧默认一致——不产生普攻候选。"""
    monkeypatch.setenv(FLAG, "0")
    ai = TacticalAI(engine)
    assert ai._basic_attack_candidates() == []
    p = engine.state.player
    assert (p.speed_limit, p.mana_limit) == (4, 8)
    assert (p.effective_attack_count(), p.effective_attack_power()) == (4, 8)


def test_flag_default_enabled(engine, monkeypatch):
    """DM裁定 2026-09-10：普攻候选默认开启（不设旗标也在场）。"""
    monkeypatch.delenv(FLAG, raising=False)
    ai = TacticalAI(engine)
    assert len(ai._basic_attack_candidates()) == len(ai.alive_enemies()) >= 1


def test_flag_on_yields_one_candidate_per_live_enemy(engine, monkeypatch):
    """正常路径：旗标开启后每个存活敌人各一个普攻候选。"""
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine)
    cands = ai._basic_attack_candidates()
    assert len(cands) == len(ai.alive_enemies()) >= 1
    c = cands[0]
    assert c["kind"] == "attack" and c["label"].startswith("普攻→")
    assert [step[0] for step in c["steps"]] == ["prepare_attack", "resolve_attack"]


def test_preview_sequence_sees_real_attack_damage(engine, monkeypatch):
    """正常路径：预演整串跑完，打分看见真实伤害（当前速度4×当前法力8=32，未被闪避时）。"""
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine)
    foe = ai.alive_enemies()[0]
    strip_monster_relics(engine)          # 活血衣等回血遗物会改写 hp 差值
    foe.current_speed = 0                 # 速度0 → 不能闪避 → 伤害确定
    pv = ai.previewer.preview_sequence(ai._attack_steps(foe.name))
    assert all(r.get("success") for r in pv["results"]), pv["results"]
    hp = next(e for e in pv["diff"]["enemies"] if e["name"] == foe.name)
    p = engine.state.player
    assert hp["hp_before"] - hp["hp_after"] == \
        p.effective_attack_count() * p.effective_attack_power() == 32, hp
    # 真实状态分毫不动（预演是副本执行）
    assert foe.current_hp == hp["hp_before"]


def test_empty_pool_means_zero_damage_output(engine, monkeypatch):
    """**新公式的重要后果**（DM裁定 2026-09-10：攻击力=当前法力）。

    法力归零 → 攻击力 0 → 普攻也打不出伤害。接普攻原本就是为了「法力花干还有输出」，
    而攻力=当前法力把这条兜底取消了：一池制下把法力花光，本场就彻底失去输出手段
    （若当前速度也归零，攻次和攻力都为 0，还要吃【雕塑】）。
    这条测试钉住事实，不代表它是对的——是否要给普攻保底，等 DM 裁定。
    """
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine, verbose=True)
    foe = ai.alive_enemies()[0]
    foe.current_speed = 0
    hp_before = foe.current_hp
    engine.state.player.current_mana = 0          # 一池花干
    assert engine.state.player.effective_attack_power() == 0
    ai.take_turn()
    assert foe.current_hp == hp_before, f"攻力0时普攻应为0伤，实掉 {hp_before - foe.current_hp}"


def test_full_pool_shaifa_outscores_basic_attack(engine, monkeypatch):
    """③修复（2026-09-10 法力按攻力折价）后的真实取舍——结论再次反转。

    上一轮（杀伐5X）结论「满池必选杀伐(40伤)>普攻(32伤)」已作废：那次打分
    没算「法力=攻力」的机会成本。③修复后花 8 法力 = 烧掉本场剩余全部攻力
    （攻次4 → 折价 8×(0.12+0.5×4)≈17 分），实测：
        普攻→尸霸 44.80（零耗 32 伤，且保住后续回合的攻力）
        杀伐X=8   31.12 （40 伤 − 法力折价）
    满池**第一手**首选普攻（零耗 32 伤，保住后续攻力）。

    2026-09-13 杀伐改 X² 后的续集：第一手仍是普攻，但第二手不再是空过——
    杀伐X=8 从 31.12 涨到 65.68（8 法力换 64 伤），压过法力折价后依然划算。
    先零耗打一发、再把剩下的池子一次性兑成平方伤害，正是 X² 想要的节奏。
    """
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine, verbose=True)
    ai.take_action()
    decisions = [line for line in ai.log if "实时决策" in line]
    assert decisions, decisions
    assert "普攻" in decisions[0], decisions
    kill_probe = ai._probe("杀伐")
    kill_candidates = [c["label"] for c in ai._daowen_candidates()
                       if c["label"].startswith("杀伐")]
    assert kill_probe and kill_probe.get("kind") == "damage", kill_probe
    assert ai.remaining_actions() == 1
    assert "杀伐X=8" in kill_candidates, {
        "kill_candidates": kill_candidates,
        "mana": ai.mana(),
        "remaining_actions": ai.remaining_actions(),
        "cost_per_x": kill_probe.get("cost_per_x"),
        "x_value": ai.player.dao_wen["杀伐"].x_value,
        "can_use": ai.player.dao_wen["杀伐"].can_use(),
    }
    ai.take_action()
    decisions = [line for line in ai.log if "实时决策" in line]
    assert any("杀伐" in line for line in decisions[1:]), {
        "decisions": decisions,
        "mana": ai.mana(),
        "remaining_actions": ai.remaining_actions(),
        "dao_wen": list(ai.player.dao_wen),
        "kill_probe": kill_probe,
        "kill_candidates": kill_candidates,
        "preview_rejected": ai.preview_rejected,
    }


def test_spent_pool_leaves_no_damaging_candidate(engine, monkeypatch):
    """DM 必须知道的后果：攻力=当前法力，池子花干后普攻也是 0 伤。

    实测（法力 0/8）AI 的候选里**没有任何伤害手段**，只剩 固执/残韵 这类非伤害项：
        残韵·曲解→庇护@尸霸 1.20 ；固执X=1 70.80
    即「法力归零 = 本场彻底没有输出」。DM 已明确拒绝给普攻设保底伤害
    （2026-09-10：「我觉得只是因为目前消耗法力的道纹数值不够」），故此处只记录不修改。
    """
    monkeypatch.setenv(FLAG, "1")
    p = engine.state.player
    p.current_mana = 0
    assert p.effective_attack_power() == 0, "攻力=当前法力，池空即0"
    ai = TacticalAI(engine, verbose=True)
    # 候选仍会枚举（引擎不按伤害过滤），但打分为 0，AI 不会选它——过滤发生在打分环节。
    ai.take_turn()
    decisions = [line for line in ai.log if "实时决策" in line]
    # 2026-09-17：断言收紧为**真实不变量**——普攻可以被枚举（引擎不按伤害过滤），
    # 但池空时它必须是负分，即 AI 不会选它。旧断言写的是"日志里绝不能出现普攻"，
    # 那是比意图更严的快照：只要残韵候选的并列顺序一变（如【强化】改名【全力】），
    # 普攻是否进入日志就会翻转，但它被选中的结论从未变过。
    scored = []
    for line in decisions:
        if "普攻" not in line:
            continue
        m = re.search(r"得分\s*(-?\d+(?:\.\d+)?)", line)
        scored.append(float(m.group(1)) if m else None)
    assert all(s is not None and s <= 0 for s in scored), \
        f"池空时普攻应为非正分（AI 不选它），实得 {scored}：{decisions}"
