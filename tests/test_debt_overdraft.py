"""怪物碎片代价透支成负债 + 动态还债阈值10+历史最高碎片余额 的回归锚点。

背景：引擎曾对所有碎片类代价做余额门禁——怪物碎片守恒≥0，【还债】
（规则正文：余额达到 -(10+本局历史最高碎片余额) 时触发）在 20万+ 局实跑中零触发。
修复：仅怪物侧，真碎片轨道余额不足时不再拒绝发动，shards 扣成负数（负债）。
玩家/朋友/员工/api 侧仍维持"余额不足拒绝发动"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import derivatives as derivative_rules
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Relic


def _make_combat(monster: Entity) -> CombatEngine:
    state = GameState()
    state.current_region = "罪孽都市"
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state.enemies.append(monster)
    return CombatEngine(state, DiceEngine())


def _haoduo_monster(x: int = 1, shards: int = 0, fake: int = 0) -> Entity:
    m = Entity(name="刷卡怪", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=1, attack_power=5, shards=shards, fake_shards=fake)
    m.dao_wen["豪夺"] = DaoWenInstance(DaoWen(
        name="豪夺", formula="豪夺X", cost_type="代价",
        cost_formula="5X碎片", effect_formula=""), x_value=x)
    return m


def _activate_haoduo(combat, monster):
    return combat._resolve_monster_daowen_choice(
        monster,
        {"name": "豪夺", "target_ref": "player:0", "dodge": False, "blood_shadow": False},
        {"player:0": combat.state.player}, set(), {})


def test_haoduo_is_refused_when_caster_cannot_pay_shards():
    """豪夺是「支付5X碎片去夺遗物」，不是抢碎片：自己付不起就是发不动。

    2026-11-01 用户令：支付[碎片]的额度＝真碎片＋假碎片（先扣假碎片）。
    所以"付不起"只在这种情况成立——两池合计仍不足。
    负债只由目标侧的失去碎片产生，施法者自己的碎片代价不能透支。
    """
    combat = _make_combat(_haoduo_monster(x=1, shards=0, fake=0))
    monster = combat.state.enemies[0]
    combat.state.relics.append(Relic(name="防弹插板", effect="伤害减半"))
    result = _activate_haoduo(combat, monster)
    assert result.get("daowen_skipped") == "豪夺" and result.get("reason") == "无法支付代价", result
    assert monster.shards == 0 and monster.fake_shards == 0, "付不起时两个池子都不许动"
    assert [r.name for r in combat.state.relics] == ["防弹插板"], "发不动时不得转移遗物"


def test_haoduo_is_skipped_without_stealable_relic():
    """2026-11-01：没有可夺遗物 → 不付5X碎片、不空放（跳过而非扣费后无事发生）。"""
    combat = _make_combat(_haoduo_monster(x=1, shards=100, fake=0))
    monster = combat.state.enemies[0]
    result = _activate_haoduo(combat, monster)
    assert result.get("daowen_skipped") == "豪夺", result
    assert "遗物" in result.get("reason", "")
    assert monster.shards == 100 and monster.fake_shards == 0


def test_fake_shards_pay_a_real_shard_cost_fake_first():
    """2026-11-01 用户令：假碎片是真碎片代价的合法支付来源，且优先被扣。

    真碎片3＋假碎片100、代价5 → 假碎片扣5，真碎片分文不动。
    """
    combat = _make_combat(_haoduo_monster(x=1, shards=3, fake=100))
    monster = combat.state.enemies[0]
    combat.state.relics.append(Relic(name="防弹插板", effect="伤害减半"))
    result = _activate_haoduo(combat, monster)
    assert result.get("daowen_activated") == "豪夺", result
    assert monster.fake_shards == 95 and monster.shards == 3, "应先扣假碎片，真碎片不动"
    assert all(r.get("type") != "debt_bind" for r in combat.settle_victory_paths())


def test_buymai_shard_channel_has_no_fake_shard_twin():
    """2026-11-09 用户令：假碎片途径取消，碎片只留【买命】一条（碎片1X）。

    旧实现里【碎伐】（只认真碎片）与【伪伐】（只认假碎片）是两条通道；现在只有
    【买命】一条，走通用【碎片】口径：额度＝真＋假、先扣假、真碎片补足。
    """
    spec = derivative_rules.spec_of("买命")
    assert spec.cost_kind == "碎片" and spec.rate == 1
    assert "伪伐" not in DaoWenEngine.list_all() and "碎伐" not in DaoWenEngine.list_all()
    assert DaoWenEngine.list_all().count("买命") == 1


def test_buymai_can_borrow_from_fake_shards():
    """【买命】碎片X：额度＝真+假；真碎片为0、假碎片100 时先扣假碎片。"""
    combat = _make_combat(_haoduo_monster(shards=0, fake=100))
    monster = combat.state.enemies[0]
    calc = DaoWenEngine.resolve("买命", 5, target=combat.state.player)
    combat.apply_daowen_effect("买命", calc, monster, combat.state.player)
    assert monster.fake_shards == 95 and monster.shards == 0, \
        f"应先扣假碎片，实 真{monster.shards}／假{monster.fake_shards}"


# ============ 玩家负债期的局外 0 费行动（2026-08-23 死锁修复） ============

import math

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _debt_engine(tmp_path, debt: int = -25) -> GameEngine:
    """构造：完整开局 + pre_battle 阶段 + 玩家负债（裁定D逼真负债场景）。

    冒烟批实测（2026-08-23，当时【逼债】还在）：罪孽都市逼债寄存把玩家 shards
    打到 -15/-76 后，局外所有 0费行动被 'shards < cost' 误拒（-76 < 0），
    pre_battle 精力耗尽不了=死锁。【逼债】已于 2026-10-13 删除，本用例的负债改由
    豪夺透支构造（见上），口径不变。
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
    assert e.state.shards == -25, "被拒的正费行动不得扣碎片"


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
    """负债下 0费学习(tier1)/0费改造(tier2) 可执行。"""
    e = _debt_engine(tmp_path)
    ok = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "tier": 1, "name": "封印"})
    assert ok["success"], ok
    from engine.models import Consumable
    e.state.current_region = "扭曲都市"   # 改造是扭曲都市专属行动
    e.state.consumables.append(Consumable(
        name="绷带", effect="", max_uses=3, current_uses=1))
    e.state.consumables.append(Consumable(
        name="止血粉", effect="", max_uses=2, current_uses=1))
    ok2 = e.execute_action("pre_battle_action", {
        "sub_action": "改造", "tier": 2, "names": ["绷带", "止血粉"], "target": "绷带"})
    assert ok2["success"], ok2


def test_removed_sha_qi_choice_cannot_spend_shards(tmp_path):
    """旧 choose_sha_qi 端点已删除，不能再绕过新流程扣碎片。"""
    e = _debt_engine(tmp_path, debt=30)
    e.state.pending_sha_qi_choices = ["冥煞"]  # 仅为旧存档兼容字段，不再授权该行动
    held = next(iter(e.state.player.dao_wen))
    before = e.state.shards
    result = e.execute_action("choose_sha_qi", {"sha_qi": "冥煞", "daowen_name": held})
    assert result["success"] is False
    assert e.state.shards == before, "已删除的旧端点不得扣碎片"


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
    assert "无法完整承担碎片" in str(blocked.get("error")), blocked
    assert e.state.shards == -1, "被拒选项不得扣碎片"
