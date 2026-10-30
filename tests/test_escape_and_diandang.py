"""
pytest 风格测试 - 2026-10-13 用户令：【买路财】改制逃跑行动 + 新道纹【典当】+【逼债】删除。

逃跑：轮回者行动，一次付清5次出手，以当前状态强制脱离本场战斗，
不再走DM追击裁定；最终死斗/【不离】不可逃跑。
典当X（罪孽专属，顶替逼债的闭环格）：代价失忆2X；发现获得X种道纹
（发现池=局外【学习】口径；轮回者逐组3选1领取，怪物即时随机授予）。

运行方式：
    python -m pytest tests/test_escape_and_diandang.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.gamedata import learnable_daowen_pool, learnability_error
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_escape_dd_{db_suffix}.db", rng_seed=7)
    engine.execute_action("setup_attributes",
                          {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.relics.clear()   # 保留列表身份，让死者之页绑定同步看到空遗物栏
    engine.state.energy = 3
    return engine


def _grant(engine: GameEngine, names):
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _start_with_enemy(engine: GameEngine, enemy: Entity):
    engine.state.energy = 0
    engine.execute_action("battle_start", {"relic_choices": {}})
    engine.state.enemies.clear()
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})


# ==================== 逃跑 ====================

def test_escape_pays_five_atomically_and_forces_departure():
    """正常：预算≥5时一次扣5，敌方清空、阶段回战前、不产生击杀奖励、无DM中断。"""
    engine = _new_engine("ok")
    player = engine.state.player
    _start_with_enemy(engine, Entity(name="拦路怪", entity_type="怪物",
                                     blood_limit=100, current_hp=100))
    player.add_status(StatusEffect(name="疯狂", value=3, remaining_rounds=-1, source="test"))
    assert player.action_count == 5

    r = engine.execute_action("declare_escape", {})
    assert r["success"], r
    assert r["result"]["actions_paid"] == 5
    assert player.actions_used_this_round == 5
    assert engine.state.enemies == []
    assert engine.state.phase == "pre_battle"
    assert engine.state.energy == engine.state.energy_budget
    assert not getattr(engine.state, "pending_interrupts", []), "逃跑不再产生DM追击中断"


def test_escape_rejected_by_buli_and_final_duel():
    """错误输入：【不离】生效中/最终死斗中不可逃跑，且不扣出手。"""
    engine = _new_engine("buli")
    player = engine.state.player
    _start_with_enemy(engine, Entity(name="拦路怪", entity_type="怪物",
                                     blood_limit=100, current_hp=100))
    player.add_status(StatusEffect(name="疯狂", value=3, remaining_rounds=-1, source="test"))
    engine.state.relics.append(Relic(name="不离", effect=""))
    r = engine.execute_action("declare_escape", {})
    assert r["success"] is False
    assert "不离" in r["error"]
    assert player.actions_used_this_round == 0

    engine.state.relics = []
    engine.state.in_final_duel = True
    r2 = engine.execute_action("declare_escape", {})
    assert r2["success"] is False
    assert "死斗" in r2["error"]
    assert player.actions_used_this_round == 0


def test_escape_undeploys_employees_and_resets_retreat_marks():
    """正常：逃跑沿用原买路财清理口径——员工撤部署、撤退标记重置。"""
    engine = _new_engine("cleanup")
    player = engine.state.player
    _start_with_enemy(engine, Entity(name="拦路怪", entity_type="怪物",
                                     blood_limit=100, current_hp=100))
    emp = Entity(name="雇员甲", entity_type="员工", blood_limit=40, current_hp=40)
    emp.is_deployed = True
    emp.deployed_at_round = 2
    emp.has_retreated = True
    engine.state.employees.append(emp)
    player.add_status(StatusEffect(name="疯狂", value=3, remaining_rounds=-1, source="test"))

    r = engine.execute_action("declare_escape", {})
    assert r["success"], r
    assert emp.is_deployed is False and emp.deployed_at_round == 0
    assert emp.has_retreated is False


# ==================== 典当 ====================

def test_diandang_x2_forgets_4_and_pends_2_groups_with_capacity_gate():
    """正常+边界：典当X=2 失忆4种、pending 两组；承载满时拒收不消费，腾额后可重试。"""
    engine = _new_engine("dd2")
    player = engine.state.player
    # 现行承载按死者之页计算，与血限无关；用2页小预算构造满载边界。
    player.death_page_count = 2
    assert player.daowen_capacity == 2
    _grant(engine, ["典当", "庇护", "再生", "束缚"])   # 连初始杀伐共5种
    assert len(player.dao_wen) == 5
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.append(m)
    engine.state.phase = "in_combat"

    r = engine.execute_action("use_daowen", {"daowen_name": "典当", "x": 2})
    assert r["success"], r
    assert list(player.dao_wen) == ["典当"], "失忆4种应按持有顺序忘记典当以外的全部"
    queue = engine.state.pending_diandang_discoveries
    assert len(queue) == 2

    # 第一组领取成功（1→2种，未超承载）
    first = queue[0][0]
    r2 = engine.execute_action("choose_diandang_daowen", {"daowen_name": first})
    assert r2["success"], r2
    assert first in player.dao_wen
    assert len(engine.state.pending_diandang_discoveries) == 1

    # 第二组：承载已满 → 拒收且本组保留
    # 取本组里仍然可学的一员（池子随道纹增减而变，不能假定固定下标就是合法项）
    second = next(n for n in engine.state.pending_diandang_discoveries[0]
                  if n not in player.dao_wen)
    r3 = engine.execute_action("choose_diandang_daowen", {"daowen_name": second})
    assert r3["success"] is False
    assert "承载" in r3["error"]
    assert len(engine.state.pending_diandang_discoveries) == 1

    # 增加一张死者之页腾出额度后重试成功。
    player.death_page_count = 3
    r4 = engine.execute_action("choose_diandang_daowen", {"daowen_name": second})
    assert r4["success"], r4
    assert second in player.dao_wen
    assert engine.state.pending_diandang_discoveries == []


def test_diandang_discovery_pool_matches_learnability():
    """发现池严格等于局外【学习】口径：候选一律可学、已持有/异副本专属不出池。"""
    engine = _new_engine("pool")
    player = engine.state.player
    _grant(engine, ["典当", "庇护"])
    engine.state.phase = "in_combat"
    r = engine.execute_action("use_daowen", {"daowen_name": "典当", "x": 1})
    assert r["success"], r
    group = engine.state.pending_diandang_discoveries[0]
    assert 1 <= len(group) <= 3
    owned = set(player.dao_wen)
    known = DaoWenEngine.list_all()
    pool = set(learnable_daowen_pool(owned=owned, region=engine.state.current_region,
                                    known=known))
    for name in group:
        assert name not in owned, f"{name} 已持有，不应出池"
        assert name in pool, f"{name} 不在学习口径池内"
        assert learnability_error(name, owned=owned, region=engine.state.current_region,
                                  known=known) is None


def test_diandang_stale_group_discarded_without_consuming():
    """边界：整组候选在领取前已全部获得 → 本组作废（成功返回，不报错不卡死）。"""
    engine = _new_engine("stale")
    player = engine.state.player
    _grant(engine, ["典当", "庇护"])
    engine.state.phase = "in_combat"
    r = engine.execute_action("use_daowen", {"daowen_name": "典当", "x": 1})
    assert r["success"], r
    group = list(engine.state.pending_diandang_discoveries[0])
    # 模拟发现之后通过其他途径获得全部候选
    for n in group:
        player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    r2 = engine.execute_action("choose_diandang_daowen", {"daowen_name": group[0]})
    assert r2["success"], r2
    assert r2["result"]["discarded_group"] == group
    assert engine.state.pending_diandang_discoveries == []


def test_diandang_reject_wrong_choice_lists_valid():
    """错误输入：所选不在本组 → 报错并给出合法候选，不消费本组。"""
    engine = _new_engine("wrong")
    _grant(engine, ["典当", "庇护"])
    engine.state.phase = "in_combat"
    r = engine.execute_action("use_daowen", {"daowen_name": "典当", "x": 1})
    assert r["success"], r
    group = list(engine.state.pending_diandang_discoveries[0])
    # 【典当】自身已持有，永不出池 → 必不在组
    assert "典当" not in group
    r2 = engine.execute_action("choose_diandang_daowen", {"daowen_name": "典当"})
    assert r2["success"] is False
    assert r2.get("choices") == group
    assert len(engine.state.pending_diandang_discoveries) == 1
