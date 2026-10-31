"""统一 AI 入口回归测试。

AIPlayer 是对外唯一的玩家控制器。2026-09-29 用户令：不用规则型 AI，
全部决策（含轮回者自己的战斗行动）交给 LLM 后端。
2026-10-02 架构收口：AIPlayer 不再继承 TacticalAI，也没有 tactical_combat 分支——
规则战术层（engine/ai_tactics.py）只作为隔离的实验/模拟工具存在。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from engine.ai_player import AIBackend, AIDecision, AIPlayer
from engine.api import GameEngine
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen


def _combat_engine(tmp_path):
    engine = GameEngine(
        db_path=str(tmp_path / "unified.db"),
        rng_seed=4,
        sealed_candidate_path=str(tmp_path / "sealed.json"),
    )
    assert engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance", {
        "resonance_type": "反转",
    })["success"]
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    assert setup["success"], setup
    assert begin_battle(engine)["success"]
    assert begin_round(engine)["success"]
    return engine


class _ScriptedLLM(AIBackend):
    """模拟 LLM：记录它看到的状态与可用动作，按脚本返回决策。"""

    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def decide(self, state, available_actions, context=""):
        self.calls.append((state, available_actions, context))
        return self.decision


def test_ai_player_is_the_combat_and_high_level_entrypoint(tmp_path):
    """同一个 AIPlayer 的战斗行动也由 LLM 后端决策，经引擎正常执行。"""
    engine = _combat_engine(tmp_path)
    engine.state.player.dao_wen.clear()
    engine.state.resonance.clear()
    engine.state.player.current_hp = 10
    enemy = engine.state.enemies[0]
    enemy.attack_count = 1
    enemy.attack_power = 12

    # 2026-10-11：招架已改为 resolve_attack 逐击参数，declare_parry 不再是可选行动。
    # 现行可选行动里「结束本回合」的规范名是 skip（rest 仅为旧调用别名，不再列出）。
    llm = _ScriptedLLM(AIDecision("skip", {}, "致命威胁，先跳过"))
    ai = AIPlayer(engine, backend=llm)
    result = ai.play_turn("战斗中评估本轮威胁")

    assert len(llm.calls) == 1, "轮回者战斗行动必须交给 LLM 后端决策"
    _, available, _ = llm.calls[0]
    offered = {a.get("action_type") for a in available.get("actions", [])}
    assert "skip" in offered and "cast" in offered
    assert "declare_parry" not in offered, "招架已不再是独立行动类型"
    assert result["result"]["success"], result
    assert result["result"]["action"] == "贾凡跳过"
    assert result["reasoning"] == "致命威胁，先跳过"
    assert len(ai.get_history()) == 1


def test_ai_player_default_does_not_use_rule_tactics(tmp_path, monkeypatch):
    """AIPlayer 与规则战术层既无继承关系，也不会调用其打分入口。"""
    engine = _combat_engine(tmp_path)
    from engine.ai_tactics import TacticalAI

    assert not issubclass(AIPlayer, TacticalAI), "AIPlayer 不得继承 TacticalAI"
    assert not hasattr(AIPlayer, "tactical_combat"), "tactical_combat 分支已删除"

    def _forbidden(self, *a, **k):
        raise AssertionError("正式 AI 不得调用规则战术层")

    monkeypatch.setattr(TacticalAI, "take_action", _forbidden)
    llm = _ScriptedLLM(AIDecision("rest", {}, "蓄锐"))
    ai = AIPlayer(engine, backend=llm)
    result = ai.play_turn()
    assert result["result"]["success"], result
    assert len(llm.calls) == 1
