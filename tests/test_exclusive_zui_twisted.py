"""
罪孽都市（点金/典当/豪夺/假钞）与扭曲都市现行机制，以及逆命筹和退役道纹护栏。原道纹【赌命】已改为遗物【灾厄轮盘】，专项见 test_zui_ledger_migration。
2026-10-13：【逼债】删除，【典当】顶替其闭环格。
- 正常：按《副本/罪孽都市.md》《副本/扭曲都市.md》定义结算
- 边界：碎片不足/无遗物/无碎片/反噬致死/退化归零等
- 错误：假碎片不足/碎片不足被拒绝
"""
import os
import sys
import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable, Entity, DaoWen, DaoWenInstance, Relic, StatusEffect
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.daowen import DaoWenEngine
from engine.models import GameState
from tests.monster_phase_support import resolve_monster_phase


def _setup(region="罪孽都市", mana=100, speed_limit=99):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = region
    engine.state.phase = "in_combat"
    player = engine.state.player
    player.current_mana = mana
    player.speed_limit = speed_limit
    player.current_speed = speed_limit
    player.attack_power = 5  # 默认面板攻击力1，测试固定为5以便断言伤害
    engine.state.phase = "in_combat"
    # 2026-10-13：遗物池删2件后同一种子开局遗物漂移（血誓戒→万钧印，万钧印改向攻击
    # 会干扰普攻/反噬断言）；本文件无用例依赖开局随机遗物，统一清空以钉死环境。
    engine.state.relics = []
    return engine


def _grant(engine, names):
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _add_monster(engine, name="靶怪", hp=100, atk=5, shards=0, **kw):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=atk, **kw)
    m.shards = shards
    engine.state.enemies.append(m)
    return m


def _apply_monster_daowen(engine, caster, name, x, target=None):
    target = target or caster
    calc = DaoWenEngine.resolve(name, x, target=target, caster=caster)
    return engine.combat.apply_daowen_effect(name, calc, caster, target)


# ==================== 失忆（2026-09-28 由【点金】改名改值；前身【洗劫】已废弃）====================
# 道纹【失忆】：消耗8X法力 → 直接获得80X真碎片，与伤害彻底脱钩。
# 旧"血限扣减/印记/清算联动"本版本不存在；状态【洗劫】及其"造成伤害时夺取等量碎片"
# 机制**保留**，但已不由道纹发放，只剩【帮派令】在[战始]发放。


# 2026-10-11 用户裁定：【点金】才是要的道纹（代价：失忆X；获得80X碎片），
# 2026-09-28 那次「改名为失忆 + 改成消耗8X法力」的裁定作废。原用例
# `test_normal_shiyi_converts_mana_to_shards` 钉的是作废口径，与
# `tests/test_dianjin_cost.py::test_dianjin_pays_amnesia_not_mana_and_gains_shards`
# 互斥，故删除；现役口径由后者与 `test_boundary_shiyi_insufficient_mana_rejected` 覆盖。



def test_boundary_shiyi_insufficient_mana_rejected():
    """边界：法力不够付 8X 时应当被拒绝，而不是半结算。"""
    engine = _setup(mana=15); _grant(engine, ["失忆"])  # 失忆X=2 需 8X=16，15 点不够
    player = engine.state.player
    _add_monster(engine, shards=20)
    shard0 = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "失忆", "x": 2, "target": None})
    assert r["success"] is False
    assert engine.state.shards == shard0 and player.current_mana == 15


# 2026-09-28：【点金】已正式改名为【失忆】，旧名不再接受（避免出现双注册与
# diff_project_daowen 报警）。如果需要旧存档兼容，可在 use_daowen 层做 alias。
def test_legacy_dianjin_name_rejected():
    engine = _setup(); _grant(engine, ["失忆"])
    m = _add_monster(engine, shards=20)
    r = engine.execute_action("use_daowen", {"daowen_name": "点金", "x": 1, "target": m.name})
    assert r["success"] is False, "旧名【点金】已废弃，必须用新名【失忆】"


def test_xijie_status_still_steals_shards_on_damage():
    """状态【洗劫】机制保留：直接挂状态（帮派令口径）后，造成伤害仍夺等量碎片。"""
    # 攻力=当前法力（2026-09-10），所以把法力压到 5 才能得到可断言的每手 5 点伤害
    engine = _setup(mana=5)
    player = engine.state.player
    player.add_status(StatusEffect(name="洗劫", value=2, remaining_rounds=2, source=player.name))
    m = _add_monster(engine, shards=20)
    res = engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert m.shards == 15, f"应夺5碎片，实{m.shards}"
    assert engine.state.shards == 20 + 5


def test_boundary_xijie_status_no_shards_no_steal():
    engine = _setup(mana=5)   # 同上：法力即攻力
    player = engine.state.player
    player.add_status(StatusEffect(name="洗劫", value=1, remaining_rounds=2, source=player.name))
    m = _add_monster(engine, shards=0)
    res = engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert m.shards == 0 and engine.state.shards == 20  # 无碎片则夺取无效


def test_monster_xijie_steals_player_shards_fake_first():
    engine = _setup()
    player = engine.state.player
    engine.state.fake_shards = 10
    m = _add_monster(engine, shards=0)
    m.add_status(StatusEffect(name="洗劫", value=1, remaining_rounds=2, source=m.name))
    res = engine.combat.resolve_attack(m, player, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert engine.state.fake_shards == 5, "玩家被洗劫应先扣假碎片"  # 10-5
    assert engine.state.shards == 20


# ==================== 典当（2026-10-13 顶替【逼债】的闭环格）====================

def test_normal_diandang_forgets_2x_and_pends_discovery():
    engine = _setup(); _grant(engine, ["典当", "杀伐", "庇护"])
    player = engine.state.player
    m = _add_monster(engine, shards=10)
    r = engine.execute_action("use_daowen", {"daowen_name": "典当", "x": 1, "target": m.name})
    assert r["success"], r
    # 代价失忆2：按持有顺序优先忘记典当以外的道纹（杀伐、庇护），典当自身保留
    assert "杀伐" not in player.dao_wen and "庇护" not in player.dao_wen
    assert "典当" in player.dao_wen
    # 发现获得1种：pending 一组3选1（学习口径：已持有/异副本专属不出池）
    queue = engine.state.pending_diandang_discoveries
    assert len(queue) == 1 and 1 <= len(queue[0]) <= 3
    assert "典当" not in queue[0]
    pick = queue[0][0]
    r2 = engine.execute_action("choose_diandang_daowen", {"daowen_name": pick})
    assert r2["success"], r2
    assert pick in player.dao_wen
    assert engine.state.pending_diandang_discoveries == []


def test_boundary_diandang_rejected_when_less_than_2x_owned():
    """DM口径：失忆2X付不起（持有<2X）→ 发动前拒绝，不忘不给。"""
    engine = _setup()
    player = engine.state.player
    player.dao_wen = {}
    _grant(engine, ["典当"])
    m = _add_monster(engine)
    r = engine.execute_action("use_daowen", {"daowen_name": "典当", "x": 1, "target": m.name})
    assert r["success"] is False
    assert "失忆2无法支付" in r["error"]
    assert list(player.dao_wen) == ["典当"]
    assert engine.state.pending_diandang_discoveries == []


def test_monster_diandang_grants_instantly_without_pending():
    """怪物发动【典当】（借用/面板）：没有发现界面，即时随机授予，不进 pending。"""
    engine = _setup()
    m = _add_monster(engine, shards=0)
    m.dao_wen = {}
    from engine.models import DaoWen as _D, DaoWenInstance as _I
    for n in ("典当", "全力"):
        m.dao_wen[n] = _I(_D(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    out = _apply_monster_daowen(engine, m, "典当", 1, engine.state.player)
    kinds = [e["type"] for e in out.get("effects", [])]
    assert "amnesia" in kinds and "diandang_granted" in kinds
    assert len(m.dao_wen) == 1, f"忘记2种、即时获得1种，实{m.dao_wen.keys()}"
    assert engine.state.pending_diandang_discoveries == []


def test_boundary_battle_end_clears_sealed_relics_without_qingsuan():
    """[战终]仍清封印遗物；已删除的清算不再产生或保留挂账。"""
    engine = _setup()
    player = engine.state.player
    m = _add_monster(engine, shards=0)
    engine.state.sealed_relics = {"避风铃": 3}
    assert not hasattr(player, "_qingsuan")
    m.current_hp = 0
    m.is_alive = False
    engine.execute_action("battle_end")
    assert not hasattr(player, "_qingsuan")
    assert engine.state.sealed_relics == {}, "战终应清封印遗物"


# ==================== 豪夺（2026-09-28 由【抵扣】改名改制） ====================
# 旧【抵扣】=消耗3X法力封印遗物；新【豪夺】=消耗5X碎片夺取遗物持续X回合。

def test_normal_haoduo_steals_relic_for_x_rounds():
    engine = _setup(); _grant(engine, ["豪夺"])
    player = engine.state.player
    # 目标（敌人）持有1件遗物
    m = _add_monster(engine, shards=0)
    m.relics = [Relic(name="防弹插板", effect="伤害减半"), Relic(name="其他", effect="x")]
    engine.state.shards = 100
    shards_before = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "豪夺", "x": 2, "target": m.name})
    assert r["success"], r
    assert engine.state.shards == shards_before - 10, "消耗5X=10碎片"
    # 敌人失去防弹插板
    assert not any(r.name == "防弹插板" for r in m.relics)
    # 玩家持有防弹插板
    assert any(r.name == "防弹插板" for r in engine.state.relics), "豪夺成功后施法者应获得该遗物"
    assert "防弹插板" in engine.state.stolen_relics
    assert engine.state.stolen_relics["防弹插板"]["remaining"] == 2


def test_haoduo_no_relic_no_effect():
    engine = _setup(); _grant(engine, ["豪夺"])
    m = _add_monster(engine, shards=0)
    m.relics = []
    engine.state.shards = 100
    r = engine.execute_action("use_daowen", {"daowen_name": "豪夺", "x": 1, "target": m.name})
    # 无遗物时仍会成功（没东西可夺），但不扣遗物
    assert r["success"]
    assert engine.state.stolen_relics == {}


def test_legacy_dikou_name_rejected():
    """旧名【抵扣】已从注册表移除，use_daowen 不再受理该名字。"""
    engine = _setup(); _grant(engine, ["豪夺"])
    m = _add_monster(engine)
    m.relics = [Relic(name="回锋刀", effect="x")]
    engine.state.shards = 100
    r = engine.execute_action("use_daowen", {"daowen_name": "抵扣", "x": 1, "target": m.name})
    assert r["success"] is False, "旧名【抵扣】已废弃，必须用新名【豪夺】"


# ==================== 清算（已删除，不保留替代效果） ====================

def test_qingsuan_daowen_is_retired_and_cannot_drain_shield():
    engine = _setup(); _grant(engine, ["清算"])  # 模拟旧存档残留
    m = _add_monster(engine, shards=0)
    m.shield = 50
    r = engine.execute_action("use_daowen", {"daowen_name": "清算", "x": 2, "target": m.name})
    assert not r["success"]
    engine.combat.round_start()
    assert m.shield == 50
    assert "清算" not in DaoWenEngine._registry


# ==================== 逆命筹（原消灾转换的消耗品） ====================

def _give_nimingchou(engine, uses=1):
    item = Consumable(name="逆命筹", effect="下一次随机结算重投",
                      current_uses=uses, max_uses=1)
    engine.state.consumables.append(item)
    return item


def test_normal_nimingchou_rerolls_next_auto_roll():
    engine = _setup()
    item = _give_nimingchou(engine)
    engine.state.fake_shards = 60
    r = engine.execute_action("consume_item", {"name": "逆命筹"})
    assert r["success"], r
    assert item.current_uses == 0
    assert engine.state.fake_shards == 60, "消耗品只扣耐久，不沿用旧道纹碎片价格"
    assert engine.dice.rerolls_pending == 1
    roll = engine.dice.auto_roll("t", ["a", "b", "c"])
    assert roll["rerolled"] is True, "下一次自动随机应被重投"
    assert engine.dice.rerolls_pending == 0


def test_nimingchou_does_not_spend_real_shards():
    engine = _setup()
    _give_nimingchou(engine)
    engine.state.shards = 10
    engine.state.fake_shards = 0
    r = engine.execute_action("consume_item", {"name": "逆命筹"})
    assert r["success"], r
    assert engine.state.shards == 10
    assert engine.dice.rerolls_pending == 1


def test_nimingchou_out_of_combat_has_same_durability_cost():
    engine = _setup()
    item = _give_nimingchou(engine)
    engine.state.phase = "pre_battle"
    engine.state.shards = 10
    r = engine.execute_action("consume_item", {"name": "逆命筹"})
    assert r["success"], r
    assert item.current_uses == 0
    assert engine.state.shards == 10


def test_error_nimingchou_depleted_is_rejected_without_shard_cost():
    engine = _setup()
    _give_nimingchou(engine, uses=0)
    engine.state.shards = 4
    r = engine.execute_action("consume_item", {"name": "逆命筹"})
    assert not r["success"]
    assert engine.state.shards == 4
    assert engine.dice.rerolls_pending == 0


# ==================== 假钞 ====================

def test_normal_jiachao_gains_fake_shards():
    engine = _setup(); _grant(engine, ["假钞"])
    r = engine.execute_action("use_daowen", {"daowen_name": "假钞", "x": 2})
    assert r["success"], r
    assert engine.state.fake_shards == 20, "假钞2应获得20假碎片"
    assert engine.state.shards == 20, "真碎片不受影响"


def test_boundary_jiachao_fake_lost_first():
    engine = _setup()
    engine.state.fake_shards = 20
    engine.state.shards = 10
    engine.state.lose_shards(15)
    assert engine.state.fake_shards == 5, "应优先扣假碎片"
    assert engine.state.shards == 10


# ==================== 千荆甲（通用遗物；承接已删【爆裂】的反噬口径） ====================
# 2026-10-03 用户令：【爆裂】道纹删除，反噬语义改由遗物【千荆甲】承接
# （受到攻击伤害前，攻击者受到等量伤害）。原爆裂的持续X到期用例随道纹删除一并移除。

def test_normal_qianjingjia_reflects_before_damage():
    """正常：怪物攻击持【千荆甲】的玩家 → 攻击者先受等量伤害，攻击照常落地。"""
    engine = _setup(region="扭曲都市")
    player = engine.state.player
    engine.state.relics.append(Relic(name="千荆甲", effect=""))
    m = _add_monster(engine, hp=100, atk=5)
    hp_before = player.current_hp
    res = engine.combat.resolve_attack(m, player, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert m.current_hp == 100 - 5, f"攻击者应先失去等量生命，实差{100 - m.current_hp}"
    assert player.current_hp == hp_before - 5


def test_boundary_qianjingjia_attacker_dies_attack_cancelled():
    """边界：反噬把攻击者打死 → 本次攻击伤害不再落地（与旧【爆裂】同口径）。"""
    engine = _setup(region="扭曲都市")
    player = engine.state.player
    engine.state.relics.append(Relic(name="千荆甲", effect=""))
    hp_before = player.current_hp
    m = _add_monster(engine, hp=4, atk=5)     # 5 点反噬直接打死 4 血怪
    res = engine.combat.resolve_attack(m, player, hit_index=0, is_must_hit=True, dodge=False)
    assert not m.is_alive, "攻击者被反噬致死"
    assert res["damage_dealt"] == 0
    assert player.current_hp == hp_before, "攻击者先死，本次伤害不落地"


def test_qianjingjia_only_triggers_on_attack_damage():
    """边界：非攻击伤害（道纹直伤）不触发反噬。"""
    engine = _setup(region="扭曲都市")
    player = engine.state.player
    engine.state.relics.append(Relic(name="千荆甲", effect=""))
    m = _add_monster(engine, hp=100, atk=5)
    _apply_monster_daowen(engine, m, "杀伐", 2, target=player)
    assert m.current_hp == 100, "道纹伤害不得触发千荆甲"


def test_qianjingjia_absent_no_reflect():
    """边界：不持有遗物则无任何反噬。"""
    engine = _setup(region="扭曲都市")
    player = engine.state.player
    m = _add_monster(engine, hp=100, atk=5)
    engine.combat.resolve_attack(m, player, hit_index=0, is_must_hit=True, dodge=False)
    assert m.current_hp == 100


# ==================== 退化（扭曲都市） ====================

def test_normal_tuihua_reduces_daowen_x():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    player.add_status(StatusEffect(name="退化", value=2, remaining_rounds=-1, source="测试"))
    hp_before = m.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"], r
    # 杀伐3 退化2 → 实际 X=1 → 伤害1（X²）
    assert m.current_hp == hp_before - 1, f"退化2应使X=1(伤害1)，实减{hp_before - m.current_hp}"


def test_boundary_tuihua_zero_floor():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    player.add_status(StatusEffect(name="退化", value=10, remaining_rounds=-1, source="测试"))
    hp_before = m.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"], r
    assert m.current_hp == hp_before, "退化≥X 时数值最低为0（伤害0）"
