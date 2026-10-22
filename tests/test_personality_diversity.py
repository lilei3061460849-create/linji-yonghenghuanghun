"""千人千面·长期分化验证实验的专用测试(小规模,不在 pytest 跑 50 角色)。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim.personality_diversity_experiment as pde


# ---------- 行为证据观察器 ----------

def _mk_engine():
    from engine.api import GameEngine
    return GameEngine(db_path="/tmp/pde_t.db", rng_seed=7,
                      sealed_candidate_path="/tmp/pde_t_sealed.json",
                      death_book_path="/tmp/pde_t_book.md")


def _observer_with_player():
    from engine.models import Entity
    e = _mk_engine()
    e.state.player = Entity("甲", "轮回者", blood_limit=60, current_hp=50,
                            mana_limit=22, current_mana=20)
    e.state.current_battle = 1
    e.state.enemies.clear()
    return e, pde.BehaviorObserver(e)


def test_observer_records_risk_evidence_for_self_harm():
    e, ob = _observer_with_player()
    prev = {"hp": 50, "bl": 60, "shield": 0, "mana": 20, "enemy_hp": 100, "budget": 6}
    post = {"hp": 44, "bl": 60, "shield": 0, "mana": 19, "enemy_hp": 92}
    ob.consume(prev, post, {"success": True, "action": "发动道纹【连击X=1】",
                            "calculation": {"dao_wen": "连击", "x": 1}}, {}, 1, 0)
    dims = [(x["dim"], x["dir"]) for x in ob.recorded]
    assert ("risk_preference", 1) in dims, f"自伤行为必须记为冒险证据: {dims}"
    traits = e.state.personality_traits[e.state.player.runtime_id]["traits"]
    assert "risk_preference" in traits


def test_observer_records_safety_evidence_under_pressure():
    from engine.models import Entity
    e, ob = _observer_with_player()
    e.state.enemies.append(Entity("高压怪", "怪物", blood_limit=80, current_hp=80,
                                  attack_count=5, attack_power=6))
    prev = {"hp": 30, "bl": 60, "shield": 0, "mana": 20, "enemy_hp": 80, "budget": 6}
    post = {"hp": 30, "bl": 60, "shield": 10, "mana": 15, "enemy_hp": 80}
    ob.consume(prev, post, {"success": True, "action": "发动道纹【庇护X=5】",
                            "calculation": {"dao_wen": "庇护", "x": 5}}, {}, 1, 0)
    dims = [(x["dim"], x["dir"]) for x in ob.recorded]
    assert ("risk_preference", -1) in dims, "高压窗口无损设防应记为求稳证据"


def test_observer_evidence_cap_per_battle():
    e, ob = _observer_with_player()
    prev = {"hp": 50, "bl": 60, "shield": 0, "mana": 20, "enemy_hp": 100, "budget": 6}
    post = {"hp": 44, "bl": 60, "shield": 0, "mana": 19, "enemy_hp": 92}
    result = {"success": True, "action": "发动道纹【连击X=1】",
              "calculation": {"dao_wen": "连击", "x": 1}}
    for _ in range(6):
        ob.consume(dict(prev), dict(post), result, {}, 1, 0)
    entry = e.state.personality_traits[e.state.player.runtime_id]["traits"]["risk_preference"]
    assert entry["evidence_count"] == pde.EVIDENCE_CAP, "每场每维证据必须封顶"


# ---------- 统计函数数学正确性 ----------

def _char(cid, score, extra=None):
    traits = {}
    if score is not None:
        traits["risk_preference"] = {"score": score, "confidence": 0.8,
                                     "evidence_count": 3}
    if extra:
        traits.update(extra)
    return {"cid": cid, "seed": cid, "decisions": [], "trait_snapshots": [],
            "used_final": {}, "final_traits": traits, "cleared": 0,
            "won": False, "survived": False}


def test_trait_stats_math():
    chars = [_char(0, -0.8), _char(1, 0.0), _char(2, 0.8), _char(3, None)]
    st = pde.trait_stats(chars)["risk_preference"]
    assert st["formed"] == 3 and st["unformed"] == 1
    assert abs(st["mean"]) < 1e-9
    assert abs(st["std"] - (0.8 * (2 / 3) ** 0.5)) < 1e-3
    assert st["min"] == -0.8 and st["max"] == 0.8
    assert sum(st["histogram"].values()) == 3


def test_pairwise_distances_symmetric():
    feats = [{"attack_rate": 0.5, "defense_rate": 0.2, "finish_rate": 0.1,
              "explore_rate": 0.1, "risky_rate": 0.2, "resonance_rate": 0.0,
              "skip_rate": 0.0, "avg_mana_spend": 5, "high_pressure_defense_rate": 0.4,
              "avg_x": 3, "cleared": 3},
             {"attack_rate": 0.2, "defense_rate": 0.5, "finish_rate": 0.0,
              "explore_rate": 0.3, "risky_rate": 0.0, "resonance_rate": 0.1,
              "skip_rate": 0.1, "avg_mana_spend": 2, "high_pressure_defense_rate": 0.8,
              "avg_x": 1, "cleared": 1}]
    d = pde.pairwise_distances(feats)
    assert d["pairs"] == 1 and d["max"] == d["min"] == d["mean"] > 0


def test_verdict_grading_thresholds():
    trait = {d: {"std": 0.2} for d in pde.TRAIT_KEYS[:5]}
    beh = {"mean": 0.2}
    same_A = {"normalized_entropy": 0.7, "max_share": 0.4, "distinct": 5}
    closure_A = {"same_situation_shift_rate_vs_no_personality": 0.3}
    assert pde.verdict(trait, beh, same_A, closure_A)[0] == "A"
    same_B = {"normalized_entropy": 0.4, "max_share": 0.7, "distinct": 3}
    assert pde.verdict(trait, beh, same_B, closure_A)[0] == "B"
    weak = {d: {"std": 0.11} for d in pde.TRAIT_KEYS[:3]}
    same_C = {"normalized_entropy": 0.05, "max_share": 0.9, "distinct": 2}
    closure_C = {"same_situation_shift_rate_vs_no_personality": 0.05}
    assert pde.verdict(weak, beh, same_C, closure_C)[0] == "C"
    dead = {"x": {"std": 0.01}}
    same_D = {"normalized_entropy": 0.0, "max_share": 1.0, "distinct": 1}
    assert pde.verdict(dead, beh, same_D, closure_C)[0] == "D"


# ---------- 同局面测试 ----------

def test_same_situation_is_deterministic_for_identical_traits():
    chars = [_char(1, -0.6), _char(2, -0.6)]
    r = pde.same_situation_test(chars, steps=2)
    assert r["distinct"] == 1, "相同性格+相同经历在同局面必须同选(可复现)"


def test_same_situation_extreme_traits_diverge():
    """极端求稳 vs 极端冒险在标准高压局面应产生不同行动序列。"""
    a = _char(1, -0.85)
    b = _char(2, +0.85)
    r = pde.same_situation_test([a, b], steps=2)
    assert r["distinct"] >= 1
    # 至少不应比无性格基线更趋同
    base = pde.same_situation_test([_char(9, None)], steps=2)
    assert r["max_share"] <= base["max_share"] + 1e-9


# ---------- 端到端小规模 ----------

def test_small_run_writes_reports(tmp_path):
    report, trajectories, elapsed = pde.run_experiment(n_chars=2, battles=1)
    assert report["config"]["valid_characters"] >= 1
    assert report["total_decisions"] > 0
    assert trajectories and trajectories[0]["decisions"]
    d = trajectories[0]["decisions"][0]
    for key in ("action", "category", "risk", "behavior", "evidence"):
        assert key in d, f"轨迹缺少字段 {key}"
    assert report["verdict"]["grade"] in "ABCD"
    # 报告可序列化且包含全部十项要求的内容块
    for key in ("trait_stats", "behavior_features", "behavior_distance",
                "same_situation_with_personality", "stability", "loop_closure",
                "formulaic_scan", "verdict", "elapsed_seconds", "total_decisions"):
        assert key in report
    md = pde.render_markdown(report)
    for section in ("人格分布", "行为分布", "同局面", "稳定性", "闭环", "公式化", "证据规则"):
        assert section in md
