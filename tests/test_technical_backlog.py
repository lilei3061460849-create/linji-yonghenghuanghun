"""无需规则裁定的待办：死斗命中提交、资源超时分类。"""
from __future__ import annotations

import time

import pytest

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def duel_with_monster(tmp_path, seed=30601):
    e = GameEngine(db_path=str(tmp_path / f"duel-{seed}.db"), rng_seed=seed,
                   save_dir=str(tmp_path / "saves"),
                   sealed_candidate_path=str(tmp_path / "sealed.json"),
                   death_book_path=str(tmp_path / "deaths.md"))
    e.state.player = Entity("挑战者", "轮回者", blood_limit=300, current_hp=300,
                            mana_limit=10, current_mana=10, speed_limit=8, current_speed=8)
    lord = Entity("守擂者", "轮回者", blood_limit=100, current_hp=100,
                  mana_limit=5, current_mana=5, speed_limit=2, current_speed=2)
    monster = Entity("多攻次怪", "怪物", blood_limit=100, current_hp=100,
                     attack_count=2, attack_power=7, mana_limit=10, current_mana=10)
    # 单行动契约：一手只能是道纹或一次攻击。这里要验「命中提交随实时 attack_count
    # 漂移」，因此这只怪这一手走攻击，不再额外挂【变形】道纹。
    e.state.enemies = [lord, monster]
    e.state.in_final_duel = True
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.duel_turn = "opponent_side"
    return e


@pytest.mark.parametrize("seed", [30601, 7])
def test_duel_monster_submits_all_hits_after_shape_change(tmp_path, seed):
    from sim.duel_common import _resolve_monster_turn_one
    e = duel_with_monster(tmp_path, seed)
    result, acted_ref = _resolve_monster_turn_one(e, {"enemy:0"})
    assert result.get("success"), result
    assert acted_ref == "enemy:1"
    assert e.state.enemies[1].attack_count == 2
    assert e.state.player.current_hp == 300 - 2 * e.state.enemies[1].effective_attack_power()
    assert sum("hit_index" in item for item in result["result"]["details"]) == 2


def test_real_slow_duel_reports_resource_timeout_without_a_verdict(tmp_path):
    from sim.duel_pvp import run_duel_pvp
    e = duel_with_monster(tmp_path)
    e.state.enemies = e.state.enemies[:1]
    e.state.combat_subphase = "await_round_start"
    e.state.duel_turn = "player_side"
    calls = []

    def slow_action():
        calls.append(1)
        time.sleep(0.25)
        return True

    # 预算必须大于「进入内层循环之前的那段准备」（回始 start_round 等），
    # 否则机器一忙，墙钟会抢在第一次出手之前就到点，calls 空、断言假失败
    # （2026-10-31 全量并行时实测复现）。0.2s 对准备阶段足够宽，慢行动
    # 0.25s 仍必然越界——断言口径不变：真的执行了慢行动，且只报超时不判胜负。
    result = run_duel_pvp(e, slow_action, use_tactical=False, max_wall_seconds=0.2,
                          max_rounds=2, max_steps=5)
    assert calls, "必须真的执行慢行动，不只是把墙钟改成零"
    assert result.get("timeout") and result.get("timeout_kind") == "wall_clock", result
    assert result.get("winner") is None


def test_build_summary_separates_resource_timeouts_from_invalid_and_loss():
    from sim.build_learner import summarize_build_results
    complete = {"cleared": 7, "won": True, "duel_fought": True,
                "duel_won": True, "invalid": False}
    timeout = {"cleared": 7, "won": False, "duel_fought": True,
               "duel_timeout": True, "invalid": False}
    result = summarize_build_results([complete, timeout], invalid=1)
    assert result["valid"] == 1 and result["invalid"] == 1, result
    assert result["timeouts"] == 1
    assert result["full_rate"] == 1.0 and result["pvp_rate"] == 1.0


def test_duel_recovers_real_hit_schema_drift_without_double_execution(tmp_path, monkeypatch):
    from sim.duel_common import _resolve_monster_turn_one
    e = duel_with_monster(tmp_path)
    execute = e.execute_action
    errors = []

    def drifting_snapshot(action, params):
        result = execute(action, params)
        if action == "prepare_monster_phase" and result.get("success"):
            # 故障注入只改变现场面板；报错、事务回滚和最终提交全部由真实引擎处理。
            e.state.enemies[1].attack_count += 1
        elif action == "resolve_monster_phase" and not result.get("success"):
            errors.append(result["error"])
        return result

    monkeypatch.setattr(e, "execute_action", drifting_snapshot)
    result, ref = _resolve_monster_turn_one(e, {"enemy:0"})
    assert errors and "必须提交3次命中" in errors[0]
    assert result.get("success"), result
    assert ref == "enemy:1"
    details = result["result"]["details"]
    assert sum("hit_index" in item for item in details) == 3
    # 攻击提交里不应出现道纹结算（防止重复执行同一出手）。
    assert sum("daowen_activated" in item for item in details) == 0
    assert e.state.player.current_hp == 300 - 3 * e.state.enemies[1].effective_attack_power()


def test_play_does_not_reclassify_a_timeout_as_invalid_or_log_a_loss(monkeypatch):
    import sim.build_learner as bl
    monkeypatch.setattr(bl, "_play", lambda *args, **kwargs: {
        "cleared": 7, "won": False, "invalid": False, "duel_timeout": True,
        "duel_fought": True})
    telemetry = {}
    result = bl.play("杀伐", [], "乱葬岗", telemetry=telemetry)
    assert result["invalid"] is False and result["timeout_kind"] == "wall_clock"
    assert not telemetry.get("outcomes")


def test_evaluation_records_resource_timeouts_separately(monkeypatch):
    import sim.build_learner as bl
    samples = iter([
        {"cleared": 7, "won": True, "duel_fought": True, "duel_won": True},
        {"cleared": 7, "won": False, "duel_timeout": True, "reason": "pvp_timeout"},
        {"cleared": 0, "won": False, "invalid": True, "reason": "schema error"},
    ])
    monkeypatch.setattr(bl, "play", lambda *args, **kwargs: next(samples))
    telemetry = {}
    result = bl.evaluate_build("杀伐", [], 3, 1, telemetry=telemetry)
    assert (result["valid"], result["invalid"], result["timeouts"]) == (1, 1, 1)
    assert result["full_trials"] == 1 and result["full_rate"] == 1.0
    assert telemetry["timeout_reasons"] == {"pvp_timeout": 1}
    assert telemetry["invalid_reasons"] == {"schema error": 1}
    assert telemetry["outcomes"]["loss"] == 0


def test_duel_lab_excludes_timeouts_from_win_rate(tmp_path, monkeypatch):
    import json
    from sim import b19_duel_lab as lab

    kb = tmp_path / "kb.json"
    ecology = tmp_path / "eco.json"
    output = tmp_path / "result.json"
    kb.write_text("{}")
    ecology.write_text("{}")
    monkeypatch.setattr(lab, "LAB", {"sealed_path": str(tmp_path / "seal.json")})
    monkeypatch.setattr(lab, "ecology_builds", lambda path: [])
    monkeypatch.setattr(lab.bl, "learned_policy", lambda knowledge: {})
    monkeypatch.setattr(lab.bl, "build_scoreboard", lambda knowledge, n: [(1.0, ("杀伐", ()))])
    samples = iter([
        {"cleared": 7, "duel_fought": True, "duel_won": True, "duel_rounds": 2},
        {"cleared": 7, "duel_fought": True, "duel_timeout": True, "duel_rounds": 3},
        {"invalid": True, "reason": "schema error"},
    ])
    monkeypatch.setattr(lab.bl, "play", lambda *args, **kwargs: next(samples))
    lab.run_eval(7, 3, str(ecology), str(output), str(kb))
    row = json.loads(output.read_text())["rows"][0]
    assert row["duels"] == row["won"] == 1 and row["wr"] == 1.0
    assert row["timeouts"] == row["invalid"] == 1
    assert row["lost_kill"] == 0 and row["rounds_avg"] == 2


def test_probe_timeout_gate_logs_it_without_calling_it_invalid(capsys):
    from sim.build_learner import _skip_resource_timeout
    assert _skip_resource_timeout({"duel_timeout": True}, context="test-probe")
    output = capsys.readouterr().out
    assert '"sample_status": "timeout"' in output and '"context": "test-probe"' in output
    assert "invalid" not in output
    assert not _skip_resource_timeout({"won": False})
