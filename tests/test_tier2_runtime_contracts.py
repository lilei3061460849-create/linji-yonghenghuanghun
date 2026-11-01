"""二阶现运行时的机械验收：不是通关率实验，也不反推属性预算。"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.api import GameEngine
from engine.monsters import compute_draw_cap
from sim.build_learner import _resolve_monster_turn
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices
from tests.setup_support import finish_initial_daowen


def tier2_engine(tmp_path, seed):
    e = GameEngine(db_path=str(tmp_path / f"tier2-{seed}.db"), rng_seed=seed,
                   save_dir=str(tmp_path / "saves"),
                   sealed_candidate_path=str(tmp_path / "sealed.json"),
                   death_book_path=str(tmp_path / "deaths.md"))
    assert e.execute_action("setup_attributes", {
        "name": "二阶接口验收", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    finish_initial_daowen(e, prefer="杀伐", only_prefer=True)
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    result = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    assert result["success"], result
    from sim.build_learner import _take_discovered_relic
    choice = _take_discovered_relic(e)
    assert choice["success"], choice
    e.state.energy = 0
    e.state.current_battle = 5  # 测第6场出怪/增援合同，非从零通关实验。
    result = e.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e)})
    assert result["success"], result
    return e, result


@pytest.mark.parametrize("seed", [7, 30601])
def test_tier2_spawn_and_real_multi_round_submission_contract(tmp_path, seed):
    e, start = tier2_engine(tmp_path, seed)
    plan = start["spawn_plan"]
    assert plan["cap"] == compute_draw_cap(6, 2)
    assert len(e.state.enemies) == plan["first_count"]
    assert len(e.state.monster_reinforcements) + len(e.state.enemies) == plan["total"]
    # 为了单独验证回合协议而注入耐久；不把此结果当作合法强度/胜率数据。
    e.state.player.blood_limit = e.state.player.current_hp = 100000
    for _ in range(2):
        assert e.execute_action("round_start", {
            "relic_choices": round_start_relic_choices(e)})["success"]
        # 交替出手：驱动每步只结算一个怪物 actor，并把玩家侧优先权交出去；
        # 反复推进到双方都无出手权（await_round_end）才允许回终。
        for _ in range(12):
            if e.state.combat_subphase == "await_round_end":
                break
            result = _resolve_monster_turn(e)
            assert result["success"], result
        assert all(m.mutation_count >= 0 for m in e.state.enemies)
        end = e.execute_action("round_end", {})
        assert end["success"], end
    assert e.state.current_round == 2


def test_tier2_battle_end_is_blocked_until_reinforcements_enter(tmp_path):
    e, _ = tier2_engine(tmp_path, 7)
    # 直接构造一个合法远期增援，验证战终门禁，不依赖恰好掷出某一配方。
    from engine.monsters import parse_monster_pool
    monster = parse_monster_pool(Path(__file__).resolve().parents[1] / "副本索引.md")["乱葬岗"][0]
    e.state.monster_reinforcements.append(dict(monster, arrive_round=99))
    for enemy in e.state.enemies:
        enemy.current_hp = 0
        enemy.is_alive = False
    result = e.execute_action("battle_end", {})
    assert not result["success"] and "增援" in result["error"], result
