"""
pytest 风格测试 - 配方式出怪的进场节奏（2026-09-28 用户令）

规则：N=随机(1,上界)，S=随机(1,N) 只在[战始]进场；其余按波次增援——
第 i 波在上一波之后等待 T_i=随机(1,5) 回合进场 R_i 只（一波可多只），直至 S+ΣR_i=N。
旧口径「R1只出第1只，R4/R7/R10…回始各增援1只」已废止（2026-09-11 用户令引入，
2026-09-28 用户令改为配方式）；增援怪进场当回合即可发动道纹（2026-09-15 用户令删除白板）；
增援未到齐时战终门禁拦。

事实源：`engine/monsters.py::roll_spawn_plan` 与 `engine/combat.py::round_start`。
本文件的断言全部**读引擎自己公布的配方**（battle_start 返回的 spawn_plan），
不写死随机数——验的是"照不照配方执行"，不是"骰子掷出几点"。

运行方式：
    python -m pytest tests/test_wave_spawn.py -v
"""
import os

os.makedirs("/tmp/linji_tests", exist_ok=True)
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_wave_{db_suffix}.db", rng_seed=7)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    choice = setup["result"]["relic_choices"][0]
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    return engine


def _start_battle_with_reinforcements(engine: GameEngine, tries: int = 10) -> dict:
    """把场次推到"必定可能出现增援"的位置，返回一次**有增援队列**的战始结果。

    第6场一阶上界=3，N=1 时没有增援（概率1/3）；最多重试 tries 场，
    每场都清空战场并正常结算战终，保证不污染门禁与随机流的语义。
    """
    for _ in range(tries):
        engine.state.current_battle = 5   # 下一场即第6场：一阶上界=3；不触发第7场的最终冠冕
        engine.state.energy = 0
        r = engine.execute_action("battle_start", {})
        assert r["success"] is True
        if engine.state.monster_reinforcements:
            return r
        _kill_all_spawned(engine)
        # 全灭后战斗可能已自动结算，怪物阶段不再有 token：能走就走，不能走直接重来。
        prep = engine.execute_action("prepare_monster_phase", {})
        if prep.get("success") and "result" in prep:
            engine.execute_action("resolve_monster_phase",
                                  {"token": prep["result"]["token"], "choices": []})
        if engine.state.phase == "in_combat":
            engine.state.player.damage_dealt_this_round = 1
            engine.state.player.actions_used_this_round = 1
            engine.execute_action("round_end", {})
            engine.execute_action("battle_end", {})
    raise AssertionError(f"连续 {tries} 场第7战都没出现增援队列，配方式出怪可能失效")


def _kill_all_spawned(engine: GameEngine):
    for enemy in engine.state.enemies:
        enemy.current_hp = 0
        enemy.is_alive = False


def _pass_round(engine: GameEngine, kill_first: bool = True):
    """推进一轮：回始（含增援进场）→清场→怪阶段空交→回终。玩家赛前回满血防死。

    默认先清场：本文件只验"进场节奏"，若把存活怪物留在场上就必须按快照逐击提交
    出手（那是 tests/test_monster_phase_transaction.py 的覆盖面）。
    """
    p = engine.state.player
    p.blood_limit = max(p.blood_limit, 9999)
    p.current_hp = p.blood_limit
    r = engine.execute_action("round_start", {})
    assert r["success"] is True
    if kill_first:
        _kill_all_spawned(engine)
    prep = engine.execute_action("prepare_monster_phase", {})
    assert prep["success"] is True
    # 怪阶段空交即可：本文件验的是"进场节奏"，出手细节由 tests/test_monster_phase_* 覆盖。
    rr = engine.execute_action("resolve_monster_phase",
                               {"token": prep["result"]["token"], "choices": []})
    assert rr["success"] is True, rr
    # 测试替身击杀：免凡庸（进场节奏与凡庸正交）
    p.damage_dealt_this_round = 1
    p.actions_used_this_round = 1
    re_ = engine.execute_action("round_end", {})
    assert re_["success"] is True, re_
    return r


def test_battle_start_only_first_wave_enters_and_rest_is_queued():
    """正常路径：[战始]只放 S 只进场，其余全部进增援队列，且都排在 R1 之后"""
    engine = _new_engine("queue")
    r = _start_battle_with_reinforcements(engine)
    plan = r["spawn_plan"]
    assert plan["cap"] == 3, "第6场一阶上界 = max(1, 6-3) = 3"
    assert 1 <= plan["first_count"] < plan["total"]
    assert len(engine.state.enemies) == plan["first_count"]
    assert len(engine.state.monster_reinforcements) == plan["total"] - plan["first_count"]
    assert r["draw_count"] == plan["total"]
    assert r["queued_reinforcements"] == [m["name"] for m in engine.state.monster_reinforcements]
    assert all(m["arrive_round"] > 1 for m in engine.state.monster_reinforcements), \
        "增援不可能与首发同回合进场（T_i≥1）"


def test_reinforcements_arrive_exactly_on_planned_rounds():
    """正常路径：每波增援在配方公布的回合进场，一波可进多只；不按计划外的回合进场"""
    engine = _new_engine("schedule")
    r = _start_battle_with_reinforcements(engine)
    planned = {}
    for wave in r["spawn_plan"]["waves"]:
        planned.setdefault(wave["arrive_round"], 0)
        planned[wave["arrive_round"]] += wave["count"]
    spawned_by_round: dict[int, list] = {}
    for _ in range(max(planned) + 1):
        rs = engine.execute_action("round_start", {})
        assert rs["success"] is True
        arrived = [eff["entity"] for eff in rs["result"]["effects"] if eff.get("type") == "wave_spawn"]
        if arrived:
            spawned_by_round[rs["result"]["round"]] = arrived
        _kill_all_spawned(engine)
        prep = engine.execute_action("prepare_monster_phase", {})
        rr = engine.execute_action("resolve_monster_phase",
                                   {"token": prep["result"]["token"], "choices": []})
        assert rr["success"] is True
        engine.state.player.damage_dealt_this_round = 1
        engine.state.player.actions_used_this_round = 1
        engine.execute_action("round_end", {})
        if not engine.state.monster_reinforcements:
            break
    assert engine.state.monster_reinforcements == [], "全部增援最终都要进场"
    assert {rnd: len(names) for rnd, names in spawned_by_round.items()} == planned, \
        f"进场回合与数量必须与配方逐波一致：计划{planned}，实际{spawned_by_round}"


def test_reinforcement_can_use_daowen_on_arrival_round():
    """正常路径：增援怪进场当回合即可发动道纹（2026-09-15 用户令删除白板限制）

    判据取"引擎把哪些怪物列进怪阶段、给了哪些合法道纹"：增援怪在进场当回合就必须
    出现在 actors 里，且拿到的是自己面板上的道纹候选；不再有任何"登场回合不出道纹"
    的分支。道纹的**结算细节**（击数、目标）由 tests/test_monster_phase_transaction.py 覆盖。
    """
    engine = _new_engine("whiteboard")
    r = _start_battle_with_reinforcements(engine)
    arrival = min(m["arrive_round"] for m in engine.state.monster_reinforcements)
    before = len(engine.state.enemies)
    for _ in range(arrival - 1):
        _pass_round(engine)
    r_arrival = engine.execute_action("round_start", {})
    assert r_arrival["result"]["round"] == arrival
    assert len(engine.state.enemies) > before, "到点的增援必须进场"
    arrived_names = {m.name for m in engine.state.enemies
                     if m.spawned_round == arrival}
    assert arrived_names, "进场怪应带上本回合的 spawned_round"
    prep = engine.execute_action("prepare_monster_phase", {})
    assert prep["success"] is True
    actors = [a for a in prep["result"]["actors"]]
    arrived = [a for a in actors if a["monster"] in arrived_names]
    assert arrived, (f"刚进场的增援怪必须已进入怪阶段：进场{arrived_names}，"
                     f"actors={sorted(a['monster'] for a in actors)}")
    panel = {m.name: set(m.dao_wen) for m in engine.state.enemies}
    for actor in arrived:
        opts = {o["name"] for o in actor["daowen_options"]}
        assert opts <= panel.get(actor["monster"], set()), \
            f"{actor['monster']}的道纹候选必须来自自己面板：{opts} ⊄ {panel.get(actor['monster'])}"
    assert any(actor["daowen_options"] for actor in arrived), (
        f"进场增援怪当回合至少应有一道纹可发动（白板限制已于2026-09-15废止）："
        f"{[(a['monster'], a['daowen_options']) for a in arrived]}")
    # 验证到此为止：`prepare_monster_phase` 一次只允许一份待决快照，
    # 本例不再往下交回合（清场后重取快照会被两阶段门禁拒绝，那是它该拦的）。


def test_battle_end_blocked_while_queue_pending():
    """门禁：增援未到齐时战终必须拦；到齐清光后放行"""
    engine = _new_engine("gate")
    engine.state.current_battle = 4   # 打到第5场即可，避免第7场触发最终冠冕换掉 state
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    pool = engine.monster_pool["扭曲都市"]
    # 手工登记一只"排在很远回合"的增援：本例验的是战终门禁，不是随机节奏，
    # 因此直接构造前置状态（合法字段 arrive_round，见 GameState.monster_reinforcements）。
    engine.state.monster_reinforcements.append(dict(pool[0], arrive_round=99))
    _kill_all_spawned(engine)
    r = engine.execute_action("battle_end", {})
    assert r["success"] is False and "增援" in r["error"]
    assert engine.state.battle_won() is False
    # 失败结算会整体回滚 state（事务层），所以必须回写引擎里的那份，而不是本地变量
    engine.state.monster_reinforcements[0]["arrive_round"] = engine.state.current_round + 1
    _pass_round(engine, kill_first=True)
    assert engine.state.monster_reinforcements == [], "到点的增援应已进场"
    _kill_all_spawned(engine)
    r2 = engine.execute_action("battle_end", {})
    assert r2["success"] is True


def test_single_monster_battle_unaffected():
    """边界：B1（上界1）必然只出1只、无队列——上界=1 时配方不掷骰，行为与旧版逐位一致"""
    engine = _new_engine("solo")
    r = engine.execute_action("battle_start", {})
    assert r["draw_count"] == 1
    assert r["first_wave_count"] == 1
    assert r["spawn_plan"]["waves"] == []
    assert len(engine.state.enemies) == 1
    assert engine.state.monster_reinforcements == []
    assert r["queued_reinforcements"] == []
    _kill_all_spawned(engine)
    assert engine.execute_action("battle_end", {})["success"] is True


def test_high_tier_battles_can_field_many_more_monsters():
    """正常路径：二阶及以上上界=12（阶级递增），第1场也可能抽到多于4只

    一阶第7场的上界是4；同场次在乱葬岗（二阶）上界是12，配方允许 N 超出旧口径。
    """
    engine = _new_engine("tier2")
    engine.state.current_region = "乱葬岗"    # 绕过开局阶级门禁，只验上界差异
    engine.state.current_battle = 4
    engine.state.energy = 0
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    assert r["spawn_plan"]["cap"] == 12, "二阶副本 N 的上界应为怪物池规模 12"
    assert 1 <= r["draw_count"] <= 12


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
