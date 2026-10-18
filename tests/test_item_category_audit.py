"""三类物品审计的机械口径守护（2026-10-14，按已裁定的三类物品口径守护）。

用户定义：遗物被动；道纹/消耗品主动；道纹耗出手+资源；消耗品不耗出手但耗自身。
快照类断言写法：断言「当前已知集合」相等——修了必须同步改测试，防悄悄漂移。
"""
import inspect
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine, TWISTED_TOOL_LIBRARY
from engine.daowen import DaoWenEngine
from engine.enums import TriggerTiming
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, Relic
from tests.setup_support import finish_initial_daowen

ROOT = Path(__file__).resolve().parents[1]


def _setup(mana=100):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "in_combat"
    engine.state.player.current_mana = mana
    engine.state.relics = []  # 钉死：本文件断言与开局随机遗物无关
    return engine


def _grant(engine, names):
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _add_monster(engine, hp=100):
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=5)
    engine.state.enemies.append(m)
    return m


# ==================== 道纹：耗出手＋资源 ====================

COST_AMOUNT_KEYS = ("cost", "cost_shards", "cost_hp", "cost_blood_limit", "cost_speed",
                    "cost_mutation", "fake_cost", "real_cost", "forget_daowen",
                    "cost_speed_limit", "cost_mana_limit")  # 2026-10-14：【透支·枯竭通道】


def test_daowen_registry_every_daowen_has_positive_cost_channel():
    """40 条现行道纹 X=1 时必有金额>0 的代价通道（法力/失忆/异变/疲惫/流血/衰老/碎片/枯竭）。"""
    DaoWenEngine.register_all()
    assert len(DaoWenEngine._registry) == 40  # 删除独立【自食】并入【再生】，并移除已遗物化触发型道纹
    for name, fn in sorted(DaoWenEngine._registry.items()):
        kw = {}
        if "target" in inspect.signature(fn).parameters:
            kw["target"] = None
        if "caster" in inspect.signature(fn).parameters:
            kw["caster"] = None
        calc = DaoWenEngine.resolve(name, 1, **kw)
        amounts = [(k, calc[k]) for k in COST_AMOUNT_KEYS if k in calc]
        assert amounts, f"{name}：无代价金额键（疑似免费道纹？）"
        assert all(v is not None and v > 0 for _, v in amounts), f"{name}：代价金额非正 {amounts}"


def test_use_daowen_in_combat_costs_exactly_one_action():
    """战中发动道纹必耗 1 出手。"""
    engine = _setup()
    _grant(engine, ["杀伐"])
    _add_monster(engine)
    assert engine.state.player.actions_used_this_round == 0
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1,
                                             "target_ref": "enemy:0",
                                             "dodge": False, "blood_shadow": False,
                                             "trigger_spell_choices": {}})
    assert r["success"], r
    assert engine.state.player.actions_used_this_round == 1


def test_use_daowen_out_of_combat_only_xiaozai_pays_double_no_action():
    """局外仅消灾可发动（×2 资源，不耗出手）；其余拒绝（O1 预期行为）。"""
    engine = _setup()
    _grant(engine, ["假钞", "消灾"])
    engine.state.phase = "pre_battle"
    engine.state.fake_shards = 100
    r = engine.execute_action("use_daowen", {"daowen_name": "假钞", "x": 1,
                                             "dodge": False, "blood_shadow": False,
                                             "trigger_spell_choices": {}})
    assert not r["success"] and "只能在战斗中发动" in r["error"]
    r = engine.execute_action("use_daowen", {"daowen_name": "消灾", "x": 1,
                                             "dodge": False, "blood_shadow": False,
                                             "trigger_spell_choices": {}})
    assert r["success"], r
    assert engine.state.fake_shards == 0, "局外消灾假碎片代价×2"
    assert engine.state.player.actions_used_this_round == 0


# ==================== 消耗品：不耗出手但耗自身 ====================

def _grant_consumable(engine, name, uses=3, kind="normal", effect=""):
    item = Consumable(name=name, effect=effect or name, current_uses=uses, max_uses=uses, kind=kind)
    engine.state.consumables.append(item)
    return item


def test_consume_twisted_tool_costs_no_action_and_decrements():
    engine = _setup()
    engine.state.player.mana_limit = 100
    engine.state.player.current_mana = 0
    item = _grant_consumable(engine, "储能电池", uses=3,
                             effect=TWISTED_TOOL_LIBRARY["储能电池"][1])
    r = engine.execute_action("consume_item", {"name": "储能电池"})
    assert r["success"], r
    assert item.current_uses == 2 and engine.state.player.current_mana == 12
    assert engine.state.player.actions_used_this_round == 0


def test_consume_sculpture_costs_no_action_and_decrements():
    engine = _setup()
    item = _grant_consumable(engine, "石块", uses=2, kind="sculpture")
    r = engine.execute_action("consume_item", {"name": "石块", "mode": "shield"})
    assert r["success"], r
    assert item.current_uses == 1 and engine.state.player.shield == 20
    assert engine.state.player.actions_used_this_round == 0


def test_consume_dedicated_and_generic_cost_no_action_and_decrement():
    engine = _setup()
    engine.state.player.current_hp = 50
    jia = _grant_consumable(engine, "假钞贴", uses=2)
    # 肉块分发键是 kind 不是名字（与引擎授予口径一致，见 monster_life 癌变奖励）。
    meat = _grant_consumable(engine, "无限肉块", uses=1, kind="infinite_meat")
    assert engine.execute_action("consume_item", {"name": "假钞贴"})["success"]
    assert jia.current_uses == 1 and engine.state.fake_shards == 20
    assert engine.execute_action("consume_item", {"name": "无限肉块"})["success"]
    assert meat.current_uses == 0 and meat.is_depleted
    assert engine.state.player.current_hp == 60
    assert engine.state.player.actions_used_this_round == 0


def test_granted_but_unusable_consumables_snapshot():
    """F4 已执行（2026-10-14）：4 件全部有处理器——行为覆盖见 test_f1_f2_f4_execution。

    本快照只钉死「无发而无用项」：裸调不再报「没有已注册的效果处理器」
    （镇魂/黄符因缺参数失败，但错误必须是参数错误；赶尸铃战中裸调直接成功；
    替死鬼是被动触发，不走 consume_item，单独断言其拦截分支存在）。
    """
    engine = _setup()
    for name in ("镇魂铁钉", "黄符"):
        _grant_consumable(engine, name, uses=1)
        r = engine.execute_action("consume_item", {"name": name})
        assert "没有已注册的效果处理器" not in r.get("error", ""), (name, r)
        engine.state.consumables.pop()
    _grant_consumable(engine, "赶尸铃", uses=1)
    assert engine.execute_action("consume_item", {"name": "赶尸铃"})["success"]
    engine.state.consumables.pop()
    import inspect
    from engine.combat_parts import damage_death
    src = inspect.getsource(damage_death.DamageDeathMixin._apply_hostile_damage_inner)
    assert "替死鬼" in src


# ==================== 遗物：被动（触发/选择不耗出手） ====================

def test_relic_round_start_choice_costs_no_action():
    """回锋刀回始目标选择：打伤害但不耗出手。"""
    engine = _setup()
    player = engine.state.player
    player.speed_limit = 10
    player.current_speed = 6  # 缺口 4 → 伤害 12
    engine.state.relics = [Relic("回锋刀", "")]
    m = _add_monster(engine, hp=100)
    logs = engine.combat.process_relics(TriggerTiming.ROUND_START, {
        "relic_choices": {"回锋刀": {"enemy_index": 0}}})
    assert m.current_hp == 88, logs
    assert player.actions_used_this_round == 0


def test_scarlet_fruit_is_pure_passive_hp_may_exceed_blood_limit():
    """F1 已执行（2026-10-14）：猩红果实是纯被动——无战始决策，生命可超血限。

    旧效果「战始可选流血10、战终血限+2」已移交道纹【增殖】（见 test_zengzhi_*）。
    """
    engine = _setup()
    player = engine.state.player
    engine.state.relics = [Relic("猩红果实", "")]
    # 无决策：空 choices 战始直接通过。
    engine.combat.validate_battle_start_relic_choices({})
    logs = engine.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": {}})
    assert player.current_hp == player.blood_limit, logs
    assert "scarlet_fruit_active" not in engine.state.event_modifiers
    # 被动：回复不再按血限封顶。
    engine.state.apply_heal(player, player.blood_limit)
    assert player.current_hp == 2 * player.blood_limit
    # 钳制也不削溢出。
    engine.combat.clamp_immortal_body(player)
    assert player.current_hp == 2 * player.blood_limit
    # 封印后豁免消失。
    engine.state.sealed_relics["猩红果实"] = 1
    engine.combat.clamp_immortal_body(player)
    assert player.current_hp == player.blood_limit


def test_pool_relic_text_snapshot():
    """遗物池文本快照：26 件、无持续X/自选X；含「可」的遗物集合固定（F3 池内子集）。"""
    defs = GameEngine.RELIC_DEFS
    assert len(defs) == 26
    names = [n for n, _ in defs]
    assert len(set(names)) == 26
    for n, fx in defs:
        assert not re.search(r"持续\d*X", fx), f"{n}：遗物不得有持续X"
        assert "自选" not in fx, f"{n}：遗物数值不得自选"
    with_ke = {n for n, fx in defs if "可" in fx}
    assert with_ke == {"同魂笔", "血契"}, with_ke


def test_pool_relics_all_in_index():
    """遗物池每件都必须在物品索引有条目（防池与索引漂移）。"""
    text = (ROOT / "物品索引.md").read_text(encoding="utf-8")
    for n, _ in GameEngine.RELIC_DEFS:
        assert f"### {n}" in text, f"遗物池【{n}】在物品索引无条目"
