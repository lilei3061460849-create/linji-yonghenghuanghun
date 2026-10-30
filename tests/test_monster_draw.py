"""
pytest 风格测试 - 里程碑4a：出怪系统(战始抽怪)

规则变更记录：出怪数量 2026-09-28 用户令改为**配方式**（N=随机(1,上界)，S=随机(1,N)，
R_i/T_i 随机），按阶级递增——一阶上界沿用旧"battle_number-3"（旧"确定值"口径已废止），
二阶及以上上界为怪物池规模 12。事实源 `engine/monsters.py::roll_spawn_plan`。

覆盖范围：
1. 上界与配方不变量（一阶 1/1/1/1/2/3/4 为上界序列；N=S+ΣR_i；1≤T_i≤5）
2. 只从当前副本自己的12怪物池抽取，不混入其他副本
3. 允许重复抽选同一怪物种族
4. 抽到的Entity面板(攻击次数/攻击力/血限)与道纹X值必须与规则正文一致
5. "追求者·拿走口粮"登记的强制怪物，在下一场[战始]时真正额外加入敌方

不在本文件覆盖范围：战斗背景(纯叙事，本身无机制，故不需要测试)。

运行方式：
    python -m pytest tests/test_monster_draw.py -v
"""
import os
from tests.setup_support import finish_initial_daowen
os.makedirs("/tmp/linji_tests", exist_ok=True)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.dice import DiceEngine
from engine.dice import DiceEngine
from engine.monsters import compute_draw_cap, parse_monster_pool, roll_spawn_plan


DUNGEON_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "副本索引.md")


def _new_engine(db_suffix: str, region: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_draw_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_draw_cap_tier1_follows_battle_sequence():
    """正常路径：一阶 N 的上界 = max(2, 战斗场数-3)，七场为 2/2/2/2/2/3/4

    2026-09-28 用户令改配方式出怪后，这串数字的语义从"确定值"变成"随机上界"
    （N=随机(1,上界)）；旧"数量=战斗场数-3"精确口径已废止。
    2026-10-12 用户令「不再像现在这样固定数量，而是全随机」，按其选定的最小改动
    口径：只去掉前四场「恒为1只」的确定段（下界 1→2），第 6 场起的梯度不变。
    """
    expected = [2, 2, 2, 2, 2, 3, 4]
    actual = [compute_draw_cap(n, tier=1) for n in range(1, 8)]
    assert actual == expected


def test_tier1_first_battles_are_no_longer_deterministic():
    """回归：前四场不得再恒为 1 只——20 个种子里必须出现 N=2。

    这是本次改动的全部目的，若被回退成 max(1, …) 则本条立刻红。
    """
    totals = {roll_spawn_plan(DiceEngine(seed=s), 1, 1)["total"] for s in range(1, 21)}
    assert 2 in totals, f"第1场仍恒为1只：{totals}"
    assert totals <= {1, 2}, f"上界应为2：{totals}"


def test_draw_cap_scales_with_tier():
    """正常路径：阶级递增——二阶及以上 N=随机(1,12)，与战斗场数无关"""
    assert [compute_draw_cap(n, tier=2) for n in range(1, 8)] == [12] * 7
    assert compute_draw_cap(3, tier=5) == 12
    # 一阶即使场次很大也被池规模夹住（正文口径 N=随机(1,12)），不会突破上界
    assert compute_draw_cap(99, tier=1) == 12


@pytest.mark.parametrize("seed", range(1, 21))
def test_spawn_recipe_invariants(seed):
    """正常路径：配方式四条不变量对任意种子都成立

    N=随机(1,上界)、S=随机(1,N)、R_i=随机(1,N-S-ΣR_j)、T_i=随机(1,5)，直至 S+ΣR_i=N。
    """
    dice = DiceEngine(seed=seed)
    for tier in (1, 2, 4):
        for battle in range(1, 8):
            plan = roll_spawn_plan(dice, battle, tier)
            waves = plan["waves"]
            assert 1 <= plan["total"] <= plan["cap"] == compute_draw_cap(battle, tier)
            assert 1 <= plan["first_count"] <= plan["total"]
            assert sum(w["count"] for w in waves) == plan["total"] - plan["first_count"]
            assert len(plan["queue_rounds"]) == plan["total"] - plan["first_count"]
            prev = 1
            for wave in waves:
                assert 1 <= wave["count"]                        # R_i ≥ 1
                assert 1 <= wave["arrive_round"] - prev <= 5     # T_i = 随机(1,5)
                prev = wave["arrive_round"]


def test_battle_start_follows_spawn_plan():
    """正常路径：[战始]只让 S 只首发，其余按计划的进场回合进入增援队列"""
    engine = _new_engine("recipe_match", "扭曲都市")
    engine.state.current_battle = 6   # 第7场：一阶上界=4，才会出现"有增援"的局面
    engine.state.energy = 0
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    plan = r["spawn_plan"]
    assert r["draw_count"] == plan["total"] == len(engine.state.enemies) + len(plan["queue_rounds"])
    assert len(engine.state.enemies) == plan["first_count"]
    assert [m["arrive_round"] for m in engine.state.monster_reinforcements] == plan["queue_rounds"]
    assert r["reinforcement_waves"] == plan["waves"]


def test_battle_start_actually_populates_enemies_from_correct_region_pool():
    """正常路径：[战始]必须真正把敌方列表填满，且只使用当前副本自己的12怪物池"""
    engine = _new_engine("region_pool", "扭曲都市")
    pool_names = {m["name"] for m in engine.monster_pool["扭曲都市"]}
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    # 2026-10-12 用户令：一阶上界下界 1→2，第1场总数由恒为 1 变成 随机(1,2)
    assert r["draw_count"] in (1, 2), f"第1场 N 应为 随机(1,2)：{r['draw_count']}"
    assert len(engine.state.enemies) == r["first_wave_count"]
    assert 1 <= len(engine.state.enemies) <= r["draw_count"]
    assert all(e.name in pool_names for e in engine.state.enemies)
    assert all(e.entity_type == "怪物" for e in engine.state.enemies)


def test_drawn_monster_panel_matches_readme_exactly():
    """正常路径：抽到的怪物面板(攻击次数/攻击力/血限)与道纹集合必须与独立副本文档定义完全一致

    2026-09-16 用户令：面板不再写死 X，道纹段解析为 None（发动时自选），
    因此本处只校验道纹**集合**与三围是否与副本文档一致；
    X 值的自选与封顶校验见 tests/test_monster_daowen_x_free.py。
    """
    pools = parse_monster_pool(DUNGEON_INDEX_PATH)
    known = next(m for m in pools["龙心谷"] if m["name"] == "熔岩蜥")
    assert (known["attack_count"], known["attack_power"], known["blood_limit"]) == (3, 6, 42)
    # 当前独立面板仅余【波及】；原【加害】已转换为消耗品【灼罪钉】。
    assert known["dao_wen"] == {"波及": None}


def test_seven_battles_match_per_battle_draw_totals():
    """正常路径：七场连续出怪——每场总数=首发+增援，全部来自本副本池"""
    engine = _new_engine("repeat_ok", "龙心谷")
    pool_names = {m["name"] for m in engine.monster_pool["龙心谷"]}
    all_names, expected_total = [], 0
    for _ in range(7):
        engine.state.energy = 0
        r = engine.execute_action("battle_start", {})
        all_names.extend(r["enemies"] + r["queued_reinforcements"])
        expected_total += r["draw_count"]
        assert len(r["enemies"]) == r["first_wave_count"], "首发数必须等于配方里的 S"
        # 配方：按计划进场回合逐轮放出增援并清掉，否则战终门禁拦
        while engine.state.monster_reinforcements:
            engine.execute_action("round_start", {})
            for enemy in engine.state.enemies:
                enemy.current_hp = 0
                enemy.is_alive = False
            prep = engine.execute_action("prepare_monster_phase", {})
            engine.execute_action("resolve_monster_phase",
                                  {"token": prep["result"]["token"], "choices": []})
            engine.state.player.damage_dealt_this_round = 1
            engine.state.player.actions_used_this_round = 1
            engine.execute_action("round_end", {})
        for enemy in engine.state.enemies:
            enemy.current_hp = 0
            enemy.is_alive = False
        engine.execute_action("battle_end", {})
    assert len(all_names) == expected_total, "每场 N=首发+增援，七场总数应等于各场 N 之和"
    assert all(name in pool_names for name in all_names), "出怪只能来自当前副本自己的池"


def test_draws_are_with_replacement():
    """正常路径：出怪是**有放回**抽取——允许重复抽选同一怪物种族，池不被消耗

    抽屉原理：40 次抽取只可能来自 12 种，必然出现重复；若改成无放回，本例会失败。
    """
    pool = parse_monster_pool(DUNGEON_INDEX_PATH)["龙心谷"]
    dice = DiceEngine(seed=5)
    names = [dice.auto_roll(f"draw_with_replacement_{i}", pool, context="有放回抽取校验")["selected"]["name"]
             for i in range(40)]
    assert len(names) == 40
    assert len(set(names)) < len(names), "40次抽取(池仅12种)必须出现重复=有放回"
    assert set(names) <= {m["name"] for m in pool}


def test_forced_monster_from_event_appears_extra_next_battle():
    """正常路径：追求者·拿走口粮登记的怪物，必须在下一场[战始]时真正额外出现"""
    engine = _new_engine("forced_monster", "龙心谷")
    engine.state.shards = 0
    engine.event_pool.current = "追求者"
    engine.execute_action("resolve_event", {"event": "追求者", "option_id": 2})
    assert len(engine.state.forced_monsters_next_battle) == 1

    r = engine.execute_action("battle_start", {})
    names = r["enemies"]
    assert any("追求者" in n for n in names), f"追求者应额外出现，实际{names}"
    assert len(engine.state.enemies) == r["first_wave_count"] + 1, \
        "额外怪物应叠加在正常出怪数量之上，而不是占用/替换名额"
    assert engine.state.forced_monsters_next_battle == [], "登记项使用后应清空，不能在第三场重复出现"

    zhuiqiuzhe = next(e for e in engine.state.enemies if e.name == "追求者")
    assert (zhuiqiuzhe.attack_count, zhuiqiuzhe.attack_power, zhuiqiuzhe.blood_limit) == (8, 2, 96)


# ========================================================================
# 边界条件
# ========================================================================

def test_draw_cap_floors_at_two_for_small_battle_numbers():
    """边界：上界在场次很小时不能算出0或负数，必须floor在2（2026-10-12 用户令）

    下界从 1 抬到 2 之后，前四场不再是「N=1、S=1、无增援」的确定局，
    而是 N=随机(1,2)。上界为 1 才不掷骰；上界为 2 时 N 与 S 都各占一次随机流。
    """
    assert compute_draw_cap(1) == 2
    assert compute_draw_cap(2) == 2
    assert compute_draw_cap(3) == 2
    assert compute_draw_cap(0) == 2
    dice = DiceEngine(seed=11)
    plan = roll_spawn_plan(dice, 1, tier=1)
    assert plan["cap"] == 2
    assert plan["total"] in (1, 2)
    assert plan["first_count"] <= plan["total"]
    assert dice._history, "上界=2 应消耗随机流（N 与 S 各一次）"


def test_unknown_region_draws_nothing_but_does_not_crash():
    """边界：current_region不在三个已知副本池中时，不应抛异常，只是不出怪"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_draw_unknown.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.state.current_region = "尚未实现的副本"
    engine.state.phase = "pre_battle"
    engine.state.energy = 0
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    assert r["draw_count"] == 0
    assert engine.state.enemies == []


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_parse_monster_pool_never_mixes_regions():
    """非法配置校验：三个副本池互不相混，且每池严格12只"""
    pools = parse_monster_pool(DUNGEON_INDEX_PATH)
    for region in ("扭曲都市", "罪孽都市", "龙心谷"):
        assert len(pools[region]) == 12, f"{region}应有12只怪物，实际{len(pools[region])}"
    all_names = [m["name"] for region in pools.values() for m in region]
    assert len(all_names) == len(set(all_names)), "三个副本的怪物名不应互相重复/串池"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

