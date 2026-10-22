"""统一 AI 长期记忆：正常、边界、死亡清除与事实隔离回归。"""
from __future__ import annotations

import copy

from engine.ai_memory import (
    MAX_EPISODES,
    condense_to_legacy,
    create_memory,
    remember_action,
)
from engine.ai_player import AIBackend, AIDecision, AIPlayer
from engine.api import GameEngine
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen


def _engine(tmp_path, seed=7):
    engine = GameEngine(
        db_path=str(tmp_path / f"memory-{seed}.db"),
        save_dir=str(tmp_path / f"saves-{seed}"),
        sealed_candidate_path=str(tmp_path / f"sealed-{seed}.json"),
        death_book_path=str(tmp_path / f"death-{seed}.md"),
        rng_seed=seed,
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


def test_unified_ai_creates_identity_and_learns_from_real_action(tmp_path):
    """正常路径：AI 自述身份，并把真实行动结果写入经历与性格证据。"""
    engine = _engine(tmp_path)
    engine.state.player.dao_wen.clear()
    engine.state.resonance.clear()
    engine.state.player.current_hp = 10
    enemy = engine.state.enemies[0]
    enemy.attack_count = 1
    enemy.attack_power = 12
    # 法力见底：让这次不耗法力的【蓄锐】触发 resource_view 证据
    # （engine.ai_memory.infer_evidence：法力见底时仍保留低费选择）
    engine.state.player.current_mana = 0

    class _RestLLM(AIBackend):
        # 模拟 LLM 的战斗决策（2026-09-29 起战斗行动全部由 LLM 决定）
        # 2026-10-11：招架已改为逐击参数，改用【蓄锐】作为脚本决策
        def decide(self, state, available_actions, context=""):
            return AIDecision("rest", {}, "致命威胁，先蓄锐")

    ai = AIPlayer(engine, backend=_RestLLM())
    result = ai.play_turn("评估本轮威胁")

    assert result["result"]["success"], result
    memory = engine.state.player.ai_memory
    assert memory["origin_type"] == "self_authored_fiction"
    assert set(memory["identity"]) == {"origin", "regret", "value", "fear", "goal"}
    assert memory["episodes"]
    assert any(item["evidence"] for item in memory["episodes"])
    assert engine.state.personality_traits, "真实行动后应产生性格证据"


def test_memory_is_seeded_but_bounded_and_not_engine_fact(tmp_path):
    """边界/隔离：不同种子能有不同自述；记忆不会无限增长或改变数值。"""
    first = create_memory("贾凡", 1)
    second = create_memory("贾凡", 2)
    assert first["identity"] != second["identity"]

    before = {"hp": 20, "blood_limit": 20, "mana": 5, "mana_limit": 5,
              "speed": 4, "shield": 0, "enemy_hp": 30, "threat": 10}
    after = dict(before)
    result = {"success": True, "action": "resolve_attack"}
    for _ in range(MAX_EPISODES + 10):
        remember_action(first, before, after, result,
                        {"action": "resolve_attack", "label": "逐击招架"})
    assert len(first["episodes"]) == MAX_EPISODES
    assert before["hp"] == 20 and after["enemy_hp"] == 30


def test_memory_is_cleared_on_death_but_only_legacy_survives(tmp_path):
    """死亡边界：身世/经历/性格清除，审核写入的浓缩遗言才跨轮回保留。"""
    engine = _engine(tmp_path, seed=11)
    ai = AIPlayer(engine)
    ai._ensure_player_memory()
    engine.state.player.ai_memory["lessons"] = ["我终于学会先保留退路。"]
    engine.update_personality(engine.state.player, "risk_preference", -1,
                              evidence="主动选择保命")

    player = engine.state.player
    player.current_hp = 0
    player.is_alive = False
    queued = engine.execute_action("noop", {})
    assert not queued["success"]
    assert player.ai_memory == {}
    assert not engine.state.personality_traits
    draft = queued["interrupt"]["context"]["draft"]["text"]
    assert "退路" in draft

    ruling = engine.submit_ruling(
        "death_inheritance", "approve", {"action": "approve"})
    assert ruling["success"], ruling
    assert engine.state.player is None
    assert engine.state.death_book_legacies
    assert "退路" in engine.state.death_book_legacies[-1]["text"]


def test_memory_survives_save_and_load(tmp_path):
    """存档边界：当前轮回记忆随角色存档恢复，但不是独立跨轮回文件。"""
    engine = _engine(tmp_path, seed=13)
    ai = AIPlayer(engine)
    ai._ensure_player_memory()
    identity = copy.deepcopy(engine.state.player.ai_memory["identity"])
    engine.state.player.ai_memory["lessons"] = ["我会把退路留给下一次行动。"]
    saved = engine.save_game("memory")
    assert saved["success"]

    restored = GameEngine(
        db_path=str(tmp_path / "restore.db"),
        save_dir=str(tmp_path / "saves-13"),
        sealed_candidate_path=str(tmp_path / "sealed-13.json"),
        death_book_path=str(tmp_path / "death-13.md"),
        rng_seed=999,
    )
    loaded = restored.load_game("memory")
    assert loaded["success"], loaded
    assert restored.state.player.ai_memory["identity"] == identity
    assert restored.state.player.ai_memory["lessons"] == ["我会把退路留给下一次行动。"]


def test_legacy_condensation_respects_capacity():
    """非法/边界输入：浓缩遗言永远不超过引擎字数上限。"""
    memory = create_memory("超长角色", 99)
    memory["lessons"] = ["这是一条非常非常长的经历，不能把整段记忆原样带进下一轮回。"]
    text = condense_to_legacy(memory, capacity=20)
    assert 0 < len(text) <= 20
