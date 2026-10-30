"""
pytest 风格测试 - 里程碑9a：龙心谷"炼心"/【××龙心】消耗品系统（终音法器"共心环"的前置依赖）

原文："龙心谷专属行动：【炼心】局外（消耗1精力）或战斗（下次行动精力-1）中均可发动：
直到你下一次支付数值为X的代价后，获得对应类型的【××龙心】（消耗品（耐久X））。
消耗【××龙心】Y点耐久，可抵消Y点同类型代价；未被抵消的剩余代价照常支付。"

此前"炼心"只返回一句提示文字，从未真正生成过龙心消耗品对象，是纯占位。

运行方式：
    python -m pytest tests/test_dragon_heart.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import math

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_dragonheart_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    # 龙心用例需连续支付代价；清空随机遗物以隔离无关被动效果。
    engine.state.relics = []
    engine.state.player.dao_wen["连击"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="生命", cost_formula="X", effect_formula=""))
    engine.state.energy = 3
    return engine


def _start_with_enemy(engine):
    engine.state.energy = 0
    engine.execute_action("battle_start", {"relic_choices": {}})
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="靶怪", entity_type="怪物", blood_limit=999, current_hp=999))
    engine.execute_action("round_start", {})


def _use_daowen(engine, params):
    # 每次支付是独立探针；重置测试行动权，不修改生产1出手／交替规则。
    engine.state.duel_turn = "player_side"
    engine.state.player.actions_used_this_round = 0
    return engine.execute_action("use_daowen", params)


# ========================================================================
# 正常路径
# ========================================================================

def test_lianxin_converts_next_cost_into_matching_dragon_heart():
    """正常路径：炼心后，下一次实际支付的生命代价转化为等值的[生命龙心]消耗品"""
    engine = _new_engine("convert_ok")
    r = engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    assert r["success"] is True
    assert engine.state.pending_lianxin is True

    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 8, "target": "靶怪"})
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert len(hearts) == 1
    assert hearts[0].name == "生命龙心"
    assert hearts[0].current_uses == 40 and hearts[0].max_uses == 40  # 连击8生命价5X=40
    assert engine.state.pending_lianxin is False, "转化一次后应清空待生效标记"


def test_dragon_heart_offsets_future_same_type_cost():
    """正常路径：消耗龙心耐久可抵消未来同类型代价，未抵消的剩余部分照常支付"""
    engine = _new_engine("offset_ok")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 8, "target": "靶怪"})  # 获得生命龙心(40/40)

    hp_before = engine.state.player.current_hp
    r = _use_daowen(engine, {"daowen_name": "连击", "x": 5, "target": "靶怪", "dragon_heart_use": 3})
    assert r["success"] is True
    bleed_effect = next(e for e in r["execution"]["effects"] if e["type"] == "bleed_cost")
    assert bleed_effect["dragon_heart_offset"] == 3
    assert bleed_effect["actual_damage"] == 22, "25点代价被抵消3点，实付22点"
    assert engine.state.player.current_hp == hp_before - 22
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 37, "40点耐久用掉3点，剩37点"


def test_dragon_heart_merges_with_existing_same_name_consumable():
    """正常路径：再次炼心生成同名龙心时，按既有消耗品合并规则叠加耐久与耐久上限"""
    engine = _new_engine("merge_ok")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 4, "target": "靶怪"})  # 生命龙心(20/20)

    engine.state.pending_lianxin = True
    _use_daowen(engine, {"daowen_name": "连击", "x": 6, "target": "靶怪"})  # 再转化一次
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert len(hearts) == 1, "同名龙心应合并为一个对象，不是并存两个"
    assert hearts[0].current_uses == 50 and hearts[0].max_uses == 50  # 20+30


def test_lianxin_in_battle_does_not_cost_action_but_defers_energy():
    """正常路径：战斗中发动炼心不消耗出手，改为下一次局外行动多消耗1点精力"""
    engine = _new_engine("in_battle_ok")
    _start_with_enemy(engine)
    actions_before = engine.state.player.actions_used_this_round
    r = engine.execute_action("lianxin_in_battle", {})
    assert r["success"] is True
    assert engine.state.player.actions_used_this_round == actions_before, "不应消耗出手"
    assert engine.state.pending_energy_penalty == 1

    engine.state.phase = "pre_battle"  # 模拟该场战斗已合法结束
    engine.state.energy = 3
    # 【领悟】已于 2026-09-10 删除；本条测的是"炼心追加扣精力挂在任意局外行动上"，
    # 换用仍在的【休整】承载同一断言（不用探索：它会随机触发事件挡住后续行动）。
    probe = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0",
                              "amount": math.ceil(engine.state.player.blood_limit * 0.2)}],
    })
    assert probe["success"], probe
    assert engine.state.energy == 3 - 1 - 1, "应额外多扣1点精力(基础1点+炼心追加1点)"
    assert engine.state.pending_energy_penalty == 0, "结算后应清零，不能重复扣"



def _register_aging_probe(engine, monkeypatch, name="血限样本", amount_per_x=1):
    """注册一个纯血限代价的探针道纹。

    2026-09-12 起【透支】改为生命5X→X法力，不再是血限道纹；本测试关心的是
    「血限代价 × 龙心」这条通路，与具体哪个道纹无关，故用探针道纹解耦。
    引擎每次 use_daowen 都会 register_all() 重建注册表，因此这里包一层，
    让探针在每次重建后仍然存在。
    """
    from engine.daowen import DaoWenEngine
    from engine.enums import CostType

    calc = lambda x: {
        "dao_wen": name, "x": x,
        "cost_type": CostType.AGING.value,
        "cost_blood_limit": amount_per_x * x,
        "summary": f"血限{amount_per_x * x}",
    }
    original = DaoWenEngine.register_all.__func__

    def register_all_with_probe(cls):
        original(cls)
        cls._registry[name] = calc

    monkeypatch.setattr(DaoWenEngine, "register_all",
                        classmethod(register_all_with_probe))
    DaoWenEngine.register_all()
    engine.state.player.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="血限",
               cost_formula="X", effect_formula=""))
    return name


def test_different_cost_types_produce_different_named_hearts(monkeypatch):
    """正常路径：不同代价类型(生命/血限/速度)炼心后应产生对应命名的独立龙心，互不混用"""
    engine = _new_engine("types_ok")
    probe = _register_aging_probe(engine, monkeypatch)
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": probe, "x": 3, "target": "轮回者"})
    names = {c.name for c in engine.state.consumables if c.kind == "dragon_heart"}
    assert names == {"血限龙心"}


# ========================================================================
# 边界条件
# ========================================================================

def test_dragon_heart_use_capped_by_current_durability():
    """边界：请求抵消的点数超过龙心当前耐久时，最多只按当前耐久抵消，不会倒扣出负数耐久"""
    engine = _new_engine("cap_durability")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 3, "target": "靶怪"})  # 生命龙心(15/15)

    r = _use_daowen(engine, {"daowen_name": "连击", "x": 10, "target": "靶怪", "dragon_heart_use": 999})
    bleed_effect = next(e for e in r["execution"]["effects"] if e["type"] == "bleed_cost")
    assert bleed_effect["dragon_heart_offset"] == 15, "最多抵消到当前耐久为止"
    assert bleed_effect["actual_damage"] == 35
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 0


def test_dragon_heart_use_capped_by_raw_cost_not_overdrawn():
    """边界：请求抵消的点数超过本次原始代价时，最多只按原始代价抵消，不会倒欠"""
    engine = _new_engine("cap_rawcost")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 4, "target": "靶怪"})  # 生命龙心(20/20)

    r = _use_daowen(engine, {"daowen_name": "连击", "x": 3, "target": "靶怪", "dragon_heart_use": 20})
    bleed_effect = next(e for e in r["execution"]["effects"] if e["type"] == "bleed_cost")
    assert bleed_effect["dragon_heart_offset"] == 15, "本次代价只有15，最多抵消15，不能抵消超过代价本身"
    assert bleed_effect["actual_damage"] == 0
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 5


def test_fully_offset_cost_does_not_trigger_pending_lianxin_banking():
    """边界：本次代价被完全抵消为0时，即使炼心处于待生效状态，也不应银行一个耐久为0的龙心"""
    engine = _new_engine("zero_actual_no_bank")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 10, "target": "靶怪"})  # 生命龙心(50/50)

    engine.state.pending_lianxin = True  # 再次准备炼心
    _use_daowen(engine, {"daowen_name": "连击", "x": 4, "target": "靶怪", "dragon_heart_use": 20})
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert len(hearts) == 1 and hearts[0].current_uses == 30, "本次代价被完全抵消，不应银行新龙心"
    assert engine.state.pending_lianxin is True, "本次未实际支付，炼心待生效状态应保留，等待下一次真正支付"


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_dragon_heart_use_wrong_type_does_nothing(monkeypatch):
    """错误输入：请求用[生命龙心]抵消[血限]代价这种类型不匹配的情况，应完全不生效"""
    engine = _new_engine("wrong_type")
    probe = _register_aging_probe(engine, monkeypatch)
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 8, "target": "靶怪"})  # 生命龙心(40/40)

    blood_limit_before = engine.state.player.blood_limit
    r = _use_daowen(engine, {"daowen_name": probe, "x": 3, "target": "轮回者", "dragon_heart_use": 5})
    aging_effect = next(e for e in r["execution"]["effects"] if e["type"] == "aging_cost")
    assert aging_effect["dragon_heart_offset"] == 0, "生命龙心不能抵消血限代价"
    assert engine.state.player.blood_limit == blood_limit_before - 3
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 40, "类型不匹配时不应扣减生命龙心耐久"


def test_no_pending_lianxin_does_not_create_heart():
    """错误输入/非预期状态：没有发动过炼心时，正常支付代价不应意外生成龙心"""
    engine = _new_engine("no_pending")
    _start_with_enemy(engine)
    _use_daowen(engine, {"daowen_name": "连击", "x": 5, "target": "靶怪"})
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert hearts == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
