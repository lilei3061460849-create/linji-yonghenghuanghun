"""怪物碎片代价透支成负债 + 动态还债阈值10+历史最高碎片余额 的回归锚点。

背景：引擎曾对所有碎片类代价做余额门禁——怪物碎片守恒≥0，【还债】
（规则正文：余额达到 -(10+本局历史最高碎片余额) 时触发）在 20万+ 局实跑中零触发。
修复：仅怪物侧，真碎片轨道余额不足时不再拒绝发动，shards 扣成负数（负债）。
玩家/朋友/员工/api 侧仍维持"余额不足拒绝发动"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState


def _make_combat(monster: Entity) -> CombatEngine:
    state = GameState()
    state.current_region = "罪孽都市"
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state.enemies.append(monster)
    return CombatEngine(state, DiceEngine())


def _xiaozai_monster(x: int = 1, shards: int = 0, fake: int = 0) -> Entity:
    m = Entity(name="刷卡怪", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=1, attack_power=5, shards=shards, fake_shards=fake)
    m.dao_wen["消灾"] = DaoWenInstance(DaoWen(
        name="消灾", formula="消灾X", cost_type="碎片",
        cost_formula="50X假/5X真", effect_formula=""), x_value=x)
    return m


def test_monster_xiaozai_overdraws_real_shards_into_debt():
    """正常路径：怪物无碎片发动消灾——不再拒绝，真碎片扣成负债。"""
    combat = _make_combat(_xiaozai_monster(x=1, shards=0, fake=0))
    monster = combat.state.enemies[0]
    result = combat._resolve_monster_daowen_choice(
        monster, {"name": "消灾", "dodge": False, "blood_shadow": False}, {}, set(), {})
    assert result.get("daowen_activated") == "消灾" or "monster" in result
    assert monster.shards == -5, f"消灾X1 真碎片代价 5，0 余额透支应为 -5，实际 {monster.shards}"


def test_xiaozai_chain_reaches_debt_threshold_and_binds_at_settle():
    """链路：连续透支越过动态负债线→回终结算触发还债→转为员工参战。"""
    combat = _make_combat(_xiaozai_monster(x=1, shards=0, fake=0))
    monster = combat.state.enemies[0]
    # 2026-09-16：道纹递增已废止，消灾 X 恒为 1、每次真碎片代价恒为 5。
    # 历史峰值为0，动态阈值为10；两次消灾后余额达到-10。
    for _ in range(2):
        combat._resolve_monster_daowen_choice(
            monster, {"name": "消灾", "dodge": False, "blood_shadow": False},
            {}, set(), {})
        combat._monster_round_used(monster).discard("消灾")  # 跨回合重置使用记录
    assert monster.shards <= -(combat.DEBT_THRESHOLD + monster.shards_peak)
    results = combat.settle_victory_paths()
    assert any(r.get("type") == "debt_bind" for r in results), f"负债达到动态阈值应触发还债: {results}"
    assert monster.is_debt_bound is True
    assert monster.entity_type == "员工" and monster in combat.state.get_all_player_side()


def test_debt_trigger_boundary_9_not_bound_10_bound():
    """历史峰值为0时，负债9不触发、负债10正好触发。"""
    for shards, expect in ((-9, False), (-10, True)):
        combat = _make_combat(_xiaozai_monster(shards=shards))
        monster = combat.state.enemies[0]
        results = combat.settle_victory_paths()
        bound = any(r.get("type") == "debt_bind" for r in results)
        assert bound is expect, f"负债{-shards}触发期望{expect}，实际{bound}"


def test_debt_threshold_scales_with_historical_peak():
    """历史最高碎片余额100时，负债109不触发，负债110触发。"""
    for shards, expect in ((-109, False), (-110, True)):
        monster = _xiaozai_monster(shards=100)
        monster.shards = shards
        combat = _make_combat(monster)
        results = combat.settle_victory_paths()
        bound = any(r.get("type") == "debt_bind" for r in results)
        assert bound is expect, (shards, monster.shards_peak, results)
        assert monster.shards_peak == 100


def test_fake_shards_still_preferred_no_debt_when_fake_sufficient():
    """反向：假碎片充足时优先扣假，不产生负债、不触发还债。"""
    combat = _make_combat(_xiaozai_monster(x=1, shards=3, fake=100))
    monster = combat.state.enemies[0]
    combat._resolve_monster_daowen_choice(
        monster, {"name": "消灾", "dodge": False, "blood_shadow": False}, {}, set(), {})
    assert monster.fake_shards == 50 and monster.shards == 3
    assert all(r.get("type") != "debt_bind" for r in combat.settle_victory_paths())


# ============ 玩家负债期的局外 0 费行动（2026-08-23 死锁修复） ============

import math

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _debt_engine(tmp_path, debt: int = -76) -> GameEngine:
    """构造：完整开局 + pre_battle 阶段 + 玩家负债（裁定D逼真负债场景）。

    冒烟批实测（2026-08-23，当时【逼债】还在）：罪孽都市逼债寄存把玩家 shards
    打到 -15/-76 后，局外所有 0费行动被 'shards < cost' 误拒（-76 < 0），
    pre_battle 精力耗尽不了=死锁。【逼债】已于 2026-10-13 删除，本用例的负债改由
    消灾透支构造（见上），口径不变。
    裁定D口径：负债冻结的是碎片**支出**(cost>0)，0费行动不属于支出。
    """
    e = GameEngine(
        db_path=str(tmp_path / "d.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=20260823,
    )
    assert e.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": "罪孽都市"})["success"]
    e.state.shards = debt
    e.state.energy = 3
    e.state.phase = "pre_battle"
    return e


def test_debt_zero_cost_xiuxing_allowed(tmp_path):
    """负债下 0费修行(tier1) 不再被误拒；正费档(tier2=15) 仍被冻结。"""
    e = _debt_engine(tmp_path)
    # 2属性点=1法限：tier1 的 1 点兑不到面板，但**入池**是合法的，不该被误拒
    ok = e.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
    assert ok["success"], ok
    assert ok["result"]["attribute_points"] >= 1, "tier1 的 1 点应存进属性点池"
    blocked = e.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 2, "to": "mana"})
    assert not blocked["success"] and "碎片不足" in blocked["error"]
    assert e.state.shards == -76, "被拒的正费行动不得扣碎片"


def test_debt_zero_cost_xiuzheng_and_tansuo_allowed(tmp_path):
    """负债下 0费休整(tier1)/0费探索(tier1) 可执行（死锁链的另两段）。"""
    e = _debt_engine(tmp_path)
    e.state.player.current_hp = 30
    # 2026-09-10 休整改制：额度=血限×20%（ceil）
    amt1 = math.ceil(e.state.player.blood_limit * 0.2)
    ok = e.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": amt1}]})
    assert ok["success"], ok
    ok2 = e.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 1})
    assert ok2["success"], ok2
    # 探索2档(30碎片)=支出，负债仍拒
    if e.event_pool.current is None:   # 首个事件结算完才能再探索（门禁）
        pass
    e.event_pool.current = None
    e.state.pending_event_queue.clear()
    e.state.energy += 1
    blocked = e.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 2})
    assert not blocked["success"] and "碎片" in blocked["error"]


def test_debt_zero_cost_learn_and_repair_allowed(tmp_path):
    """负债下 0费学习(tier1)/0费维修(tier1) 可执行。"""
    e = _debt_engine(tmp_path)
    ok = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "tier": 1, "name": "封印"})
    assert ok["success"], ok
    from engine.models import Consumable
    e.state.current_region = "扭曲都市"   # 维修是扭曲都市专属行动
    e.state.consumables.append(Consumable(
        name="绷带", effect="", max_uses=3, current_uses=1))
    ok2 = e.execute_action("pre_battle_action", {
        "sub_action": "维修", "tier": 1,
        "allocations": [{"item_ref": "consumable:0", "amount": 1}]})
    assert ok2["success"], ok2


def test_choose_sha_qi_gate_matches_charge_10(tmp_path):
    """附煞·发现选择：门禁与收费统一10（旧门禁50会误拒持10~49碎片的玩家）。"""
    e = _debt_engine(tmp_path, debt=30)   # 持30：旧门禁50必误拒，新门禁10应放行
    e.state.pending_sha_qi_choices = ["冥煞"]
    held = next(iter(e.state.player.dao_wen))
    ok = e.execute_action("choose_sha_qi", {"sha_qi": "冥煞", "daowen_name": held})
    assert ok["success"], ok
    assert e.state.shards == 20, f"应实扣10，实际剩 {e.state.shards}"


def test_event_option_zero_cost_not_blocked_by_debt(tmp_path):
    """事件选项 0 碎片选项不被负债误拒（events.py 与 api 局外行动同口径修复）。

    未修复前：'失去0碎片'/无碎片文本的选项在 shards<0 时被
    `shard_cost > shards`(0 > -1) 误拒；负债玩家遇全收费事件（库中不存在，
    已枚举36事件复核）之外的任何事件都可能拒不出去——待结算事件门禁一切
    其它行动（api.py:742）= pre_battle 死锁。冒烟配对：修复前 event/死锁
    各1局，修复后复扫0。"""
    from engine.events import resolve_option_effect
    e = _debt_engine(tmp_path, debt=-1)
    ok = resolve_option_effect("离开，离开此地", e, "通用事件", {})
    assert not ok.get("error"), ok
    # 正碎片选项负债仍拒（支出冻结语义不变）
    blocked = resolve_option_effect("失去10碎片，获得力量", e, "通用事件", {})
    assert "碎片不足" in str(blocked.get("error")), blocked
    assert e.state.shards == -1, "被拒选项不得扣碎片"
