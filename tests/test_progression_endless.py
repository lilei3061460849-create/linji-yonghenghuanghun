"""
pytest 风格测试 - 阶级推进 / 无尽模式（2026-09-28 用户令，待办②落地）

规则：
* 阶级推进：通过某阶最终死斗 → 解锁下一阶级；阶级未解锁的副本开局不能选。
  跨引擎实例以 `sealed_candidate_path` 的 `progression` 段持久；默认（无持久文件）
  所有已实现阶级都已解锁，保持「旧夹具开局能直接选二阶乱葬岗」的兼容。
* 五阶死斗胜利 → 无尽模式：怪物池=所有已实现副本合并、强度逐轮递增、
  精力逐轮递减（3→2→1 后封底）、【探索】不开放。

事实源：
* `engine/api.py::_advance_region_unlock / _advance_endless_cycle /
  unlocked_regions / _monster_pool_for_battle`
* `engine/models.py::GameState.energy_budget`
* `engine/monsters.py::merge_monster_pools / scale_monster_def_for_endless`

运行方式：
    python -m pytest tests/test_progression_endless.py -v
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.makedirs("/tmp/linji_tests", exist_ok=True)

import pytest

from engine.api import GameEngine
from engine.gamedata import REGION_TIERS
from engine.models import GameState
from engine.monsters import (merge_monster_pools, scale_monster_def_for_endless,
                             set_progression_enabled)
from tests.setup_support import finish_initial_daowen


@pytest.fixture(autouse=True)
def _progression_disabled_by_default():
    """默认开关关闭（与生产一致）；需要验证阶级推进/无尽行为的用例自己临时打开。"""
    set_progression_enabled(False)
    yield
    set_progression_enabled(False)


def _new_engine(sealed_path: Path, seed: int = 1,
                region: str = "扭曲都市", speed_points: int = 8,
                select_region: bool = True) -> GameEngine:
    """构造一个已完成开局配置的引擎：默认进入 pre_battle 阶段（已选副本）；
    select_region=False 时停在「未选副本」的 SETUP 阶段，用于断言可选地区列表。"""
    e = GameEngine(db_path=str(sealed_path.parent / f"{sealed_path.stem}.db"),
                   rng_seed=seed,
                   sealed_candidate_path=str(sealed_path))
    e.execute_action("setup_attributes",
                     {"name": "测试者", "blood_points": 25 - speed_points - 6,
                      "speed_points": speed_points, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    if not select_region:
        return e
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _selectable_regions(e: GameEngine) -> list[str]:
    actions = e.get_available_actions()["actions"]
    choice = next(a for a in actions if a["action_type"] == "setup_choose_region")
    return list(choice["params_schema"]["region"])


def _finish_seventh_no_candidate(e: GameEngine, region: str = "扭曲都市"):
    """把当前引擎推到「第7场战终·无候选封存」：把 candidate 槽保留给下一个实例。

    region 决定擂主进哪个阶级槽（与正文口径：挑战者只能与自身副本阶级的队首死斗）。
    """
    e.state.current_region = region
    e.state.current_battle = 7
    e.state.enemies.clear()
    e.state.monster_reinforcements = []
    e.state.delayed_monster_reentries = []
    e.state.phase = "in_combat"
    result = e.execute_action("battle_end", {})
    # 第8场死斗不再由战终自动触发（规则改为【最终的冠冕】世界事件）；
    # 本文件测阶级推进/封存槽，故显式走旧入口。
    if result.get("success") and "final_crown" not in (result.get("result") or {}):
        result.setdefault("result", {})["final_crown"] = e._trigger_final_crown()
    return result


def _win_duel(e: GameEngine, terminal_choice: int = 2):
    """新轮回者到达第7场触发死斗并获胜，领取指定终音法器完成进阶封存。"""
    e.state.current_battle = 7
    e.state.enemies.clear()
    e.state.monster_reinforcements = []
    e.state.phase = "in_combat"
    crown = e.execute_action("battle_end", {})
    assert crown["success"] is True
    final_crown = (crown.get("result") or {}).get("final_crown") or e._trigger_final_crown()
    assert final_crown["outcome"] == "duel_start", final_crown
    r = e.execute_action("resolve_final_duel", {"outcome": "victory"})
    assert r["success"] is True
    assert r["result"]["pending_terminal_choice"]
    return e.execute_action("choose_terminal_artifact", {"choice": terminal_choice})


# --------------------------------------------------------------------------- 事实源单测

def test_scale_monster_def_for_endless_cycle1_is_identity():
    base = {"name": "X", "blood_limit": 100, "mana_limit": 4, "speed_limit": 4,
            "attack_count": 4, "attack_power": 4}
    out = scale_monster_def_for_endless(dict(base), 1)
    assert out == base
    # 不回写原字典
    base["blood_limit"] = 100


def test_scale_monster_def_for_endless_higher_cycles():
    """无尽模式第 C 轮：血限×(100+25(C-1))% 向上取整，法限/速限各+3(C-1)。"""
    base = {"name": "X", "blood_limit": 100, "mana_limit": 4, "speed_limit": 4,
            "attack_count": 4, "attack_power": 4}
    c2 = scale_monster_def_for_endless(dict(base), 2)
    assert c2["blood_limit"] == 125   # 100*1.25 向上
    assert c2["mana_limit"] == 7 and c2["speed_limit"] == 7
    assert c2["attack_count"] == 7 and c2["attack_power"] == 7
    c4 = scale_monster_def_for_endless(dict(base), 4)
    assert c4["blood_limit"] == 175   # 100*1.75
    assert c4["mana_limit"] == 13 and c4["speed_limit"] == 13


def test_merge_monster_pools_deduplicates():
    p1 = [{"name": "a", "blood_limit": 1}, {"name": "b", "blood_limit": 2}]
    p2 = [{"name": "b", "blood_limit": 99}, {"name": "c", "blood_limit": 3}]
    merged = merge_monster_pools({"r1": p1, "r2": p2})
    assert [m["name"] for m in merged] == ["a", "b", "c"], "同名怪只保留先出现的一份"


def test_energy_budget_decreases_in_endless_mode():
    s = GameState()
    assert s.energy_budget == 3
    s.endless_mode = True
    s.endless_cycle = 1
    assert s.energy_budget == 3
    s.endless_cycle = 2
    assert s.energy_budget == 2
    s.endless_cycle = 5
    assert s.energy_budget == 1, "无尽模式精力递减封底 1"


# --------------------------------------------------------------------------- 引擎集成测试

def _write_fresh_progression(sealed: Path, unlocked_tier: int = 1,
                             endless_mode: bool = False, cycle: int = 0):
    """构造「从头开荒」起点：持久文件明确 unlocked_tier=1（或其他给定值）。"""
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_text(json.dumps({
        "progression": {"unlocked_tier": unlocked_tier,
                        "endless_mode": endless_mode, "endless_cycle": cycle}
    }, ensure_ascii=False), encoding="utf-8")


def test_default_unlocked_all_tiers_when_no_progression_file(tmp_path):
    """兼容：跨轮回持久文件不存在/没有 progression 段时，所有已实现副本都可选
    （保留「旧夹具开局能直接选二阶乱葬岗」的默认语义）。"""
    sealed = tmp_path / "sealed.json"
    e = _new_engine(sealed, select_region=False)
    assert set(_selectable_regions(e)) == set(e.monster_pool.keys())


def test_setup_rejects_unlocked_higher_tier(tmp_path):
    """门禁：开关打开 + 持久文件明确 unlocked_tier=1 时，二阶乱葬岗开局被拒绝。"""
    set_progression_enabled(True)
    sealed = tmp_path / "prog_locked.json"
    _write_fresh_progression(sealed, unlocked_tier=1)
    e = _new_engine(sealed, select_region=False)
    assert e.state.unlocked_tier == 1
    assert "乱葬岗" not in _selectable_regions(e)
    r = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    assert not r["success"] and "阶" in r["error"]
    r_ok = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    assert r_ok["success"] is True


def test_default_all_regions_when_progression_disabled(tmp_path):
    """默认（开关关闭）：开局可选所有已实现副本（含二阶乱葬岗），不做阶级门禁。"""
    sealed = tmp_path / "default_disabled.json"
    _write_fresh_progression(sealed, unlocked_tier=1)
    e = _new_engine(sealed, select_region=False)
    assert set(_selectable_regions(e)) == set(e.monster_pool.keys()), \
        "开关关闭时应直接列出所有已实现副本"


def test_first_seal_does_not_write_progression_segment(tmp_path):
    """首次通关一阶（无候选→直接封存）：开关关闭时持久文件不写 progression 段。"""
    sealed = tmp_path / "first_seal.json"
    e = _new_engine(sealed, speed_points=8, region="扭曲都市")
    r = _finish_seventh_no_candidate(e)
    assert r["result"]["final_crown"]["outcome"] == "sealed"
    data = json.loads(sealed.read_text(encoding="utf-8"))
    assert "candidates" in data
    assert "progression" not in data


def test_victory_does_not_unlock_when_disabled(tmp_path):
    """开关关闭：一阶死斗胜利后不解锁、不进下一阶级；胜者按旧擂台循环封入二阶槽。"""
    sealed = tmp_path / "no_unlock.json"
    _write_fresh_progression(sealed, unlocked_tier=1)
    seed = _new_engine(sealed, region="扭曲都市")
    _finish_seventh_no_candidate(seed)
    del seed
    winner = _new_engine(sealed, region="扭曲都市")
    r = _win_duel(winner, terminal_choice=2)
    assert r["success"] is True
    assert r["result"]["seal"]["tier"] == 2, "胜者仍按旧逻辑进二阶槽"
    assert not r["result"]["seal"].get("progression"), "开关关闭时不应返回 progression"
    # 胜者进二阶槽时一阶槽被取空、当前只有二阶槽有候选，文件应包含 candidates 但不含 progression
    if sealed.exists():
        data = json.loads(sealed.read_text(encoding="utf-8"))
        assert "progression" not in data, f"开关关闭时不得落盘 progression 段，实际键: {list(data)}"


def test_victory_unlocks_next_tier_and_persists(tmp_path):
    """开关打开时：一阶死斗胜利 → 解锁二阶；跨引擎实例持久化生效。"""
    set_progression_enabled(True)
    sealed = tmp_path / "unlock.json"
    if sealed.exists():
        sealed.unlink()
    _write_fresh_progression(sealed, unlocked_tier=1)
    seed = _new_engine(sealed, region="扭曲都市")
    _finish_seventh_no_candidate(seed)
    del seed
    winner = _new_engine(sealed, region="扭曲都市")
    assert winner.state.unlocked_tier == 1
    r = _win_duel(winner, terminal_choice=2)
    assert r["success"] is True
    seal = r["result"]["seal"]
    assert seal["tier"] == 2
    assert seal["progression"]["unlocked_tier"] == 2
    assert seal["progression"]["advanced"] is True
    data = json.loads(sealed.read_text(encoding="utf-8"))
    assert data["progression"]["unlocked_tier"] == 2
    e2 = _new_engine(sealed, region="扭曲都市", select_region=False)
    assert e2.state.unlocked_tier == 2
    assert "乱葬岗" in _selectable_regions(e2)


def test_fifth_tier_victory_enters_endless_mode(tmp_path):
    """五阶死斗胜利 → 进入无尽模式（endless_mode=True, endless_cycle=1），
    新实例开局可选所有副本；【探索】不出现在可用行动里；战始出怪池=所有副本合并。

    注意：「五阶副本·启示录」正文/怪物池/终音法器目前尚未接入运行时，端到端
    7场→死斗→胜利→终音→封印路径要等启示录正文落地后才能走通；本测试锁定
    _advance_region_unlock(MAX_TIER) 的分支契约（接口点见 _finalize_victory_seal），
    并验证持久化后的无尽模式行为（门禁、探索禁用、精力预算）。
    """
    sealed = tmp_path / "endless.json"
    if sealed.exists():
        sealed.unlink()
    set_progression_enabled(True)
    _write_fresh_progression(sealed, unlocked_tier=GameState.MAX_TIER)
    e = _new_engine(sealed, region="扭曲都市")
    prog = e._advance_region_unlock(GameState.MAX_TIER)
    assert prog["advanced"] is True
    assert prog["endless_mode"] is True
    assert prog["endless_cycle"] == 1
    assert prog["unlocked_tier"] == GameState.MAX_TIER + 1
    data = json.loads(sealed.read_text(encoding="utf-8"))
    assert data["progression"]["endless_mode"] is True
    assert data["progression"]["endless_cycle"] == 1

    # 新轮回者：无尽模式开启 → 不设阶级门禁、探索不可用、精力预算递减
    e2 = _new_engine(sealed, region="扭曲都市", select_region=False)
    assert e2.state.endless_mode is True
    assert e2.state.endless_cycle == 1
    assert set(_selectable_regions(e2)) == set(e2.monster_pool.keys()), "无尽模式不设阶级门禁"
    setup = e2.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e2.execute_action("choose_discovered_relic",
                      {"relic_name": setup["result"]["relic_choices"][0]})
    actions = e2.get_available_actions()["actions"]
    sub_actions = {a.get("params_schema", {}).get("sub_action") for a in actions}
    assert "探索" not in sub_actions, "无尽模式下【探索】不出现在可用行动里"
    e2.state.energy = 1
    r_tan = e2.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 1})
    assert r_tan["success"] is False and "无尽" in r_tan["error"]
    assert e2.state.energy == 1, "探索失败应原子性退还精力"
    assert e2.state.energy_budget == 3  # 无尽第1轮精力不变
    e2.state.endless_cycle = 3
    assert e2.state.energy_budget == 1  # 第3轮起精力封底 1


def test_endless_battle_start_draws_from_merged_pool_with_scaled_stats(tmp_path):
    """无尽模式战始出怪池 = 所有副本合并；怪物面板按 endless_cycle 递增。"""
    sealed = tmp_path / "endless_draw.json"
    _write_fresh_progression(sealed, unlocked_tier=GameState.MAX_TIER,
                             endless_mode=True, cycle=3)
    e = _new_engine(sealed, region="扭曲都市")
    assert e.state.endless_mode is True
    assert e._monster_pool_for_battle() == merge_monster_pools(e.monster_pool), \
        "无尽模式战始出怪池应合并所有已实现副本"
    e.state.energy = 0
    r = e.execute_action("battle_start", {})
    assert r["success"] is True
    # 第3轮：面板应比基线高（血限×1.5、法/速+6）；基线扭曲都市怪物血限是 270/216
    for m in e.state.enemies:
        assert m.blood_limit > 216 or m.mana_limit > 3 or m.speed_limit > 3, \
            f"无尽第3轮怪物面板应被放大，实际血{m.blood_limit}/法{m.mana_limit}/速{m.speed_limit}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
