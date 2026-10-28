"""算出了但没人消费的道纹：统一接线回归。

同类洞：calculate 出了键 / duration 挂了状态，apply 或战斗钩子不读。
波及是原型（mark_targets 从未消费）。本文件覆盖同批接线。
"""
import os
import sys
from pathlib import Path

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, Relic, StatusEffect


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_wiring_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    # 本文件是道纹单元接线测试，直接构造合法战斗阶段，避免随机出怪干扰。
    engine.state.phase = "in_combat"
    p = engine.state.player
    # 多道纹同回合连发需要 8 手预算。DM裁定 2026-09-10 后轮回者出手固定 2 次、
    # 不再由速限换算，所以改用【疯狂】+6 把预算抬到 8——本文件测的是道纹接线，
    # 不是出手预算（预算另有 tests/test_action_budget.py 专测）。
    p.speed_limit = 4
    p.current_speed = 4
    p.current_mana = 80
    p.mana_limit = 80
    p.add_status(StatusEffect(name="疯狂", value=6, remaining_rounds=-1, source="test"))
    assert p.action_count == 8, f"夹具应给出8手预算，实{p.action_count}"
    return engine


def _give(player, name):
    player.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _monster(engine, name="靶怪", hp=100, atk=3, ap=6):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=atk, attack_power=ap)
    engine.state.enemies.append(m)
    return m


def test_boba_marks_and_unmarks_targets():
    """正常：波及X选择X个目标建立波及标记（持续∞）；再次选择解除标记。"""
    engine = _engine("boba_ok")
    p = engine.state.player
    _give(p, "波及")
    foe = Entity(name="对手", entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=6, current_speed=6)
    engine.state.enemies.append(foe)
    _monster(engine, "靶怪", hp=100, atk=3, ap=6)
    engine.execute_action("round_start", {})
    mana = p.current_mana
    refs = engine.combat._combat_entity_refs()
    targets = [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
               {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False}]
    r = engine.execute_action("use_daowen",
                              {"daowen_name": "波及", "x": 2, "dodge_targets": targets})
    assert r["success"], r
    assert r["calculation"]["mark_targets"] == 2
    assert p.current_mana == mana - 4  # 波及消耗3X→2X（2026-09-16）
    for ref in ("enemy:0", "enemy:1"):
        assert refs[ref].has_status("波及")
    assert r["dodge"]["wave_marked"] == [refs["enemy:0"].name, refs["enemy:1"].name]

    # 再次选择同一目标：解除标记（toggle）
    r2 = engine.execute_action("use_daowen",
                               {"daowen_name": "波及", "x": 2, "dodge_targets": targets})
    assert r2["success"], r2
    assert not refs["enemy:0"].has_status("波及")
    assert not refs["enemy:1"].has_status("波及")
    assert r2["dodge"]["wave_unmarked"] == [refs["enemy:0"].name, refs["enemy:1"].name]


def test_boba_spreads_damage_equally_with_random_remainder():
    """平分规则：你发动的道纹同时作用于波及目标；总数值平分，余数随机分配。"""
    engine = _engine("boba_spread")
    p = engine.state.player
    _give(p, "波及")
    _give(p, "杀伐")
    foe_a = _monster(engine, "靶怪甲", hp=100, atk=3, ap=6)
    foe_b = _monster(engine, "靶怪乙", hp=100, atk=3, ap=6)
    engine.execute_action("round_start", {})
    refs = engine.combat._combat_entity_refs()
    # 波及 X=2：恰好提交 2 个目标（想标记「敌1」须同时提交第2个目标，这里连敌0一起标；
    # 2026-10-09 起 X 下限 repealed，X=1 也合法——单目标全值）
    r = engine.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2,
        "dodge_targets": [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
                          {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False}],
    })
    assert r["success"], r
    assert refs["enemy:0"].has_status("波及") and refs["enemy:1"].has_status("波及")

    # 杀伐2X=4点总伤害作用于[目标]敌0+波及目标敌1：2+2。
    r2 = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 2, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False,
    })
    assert r2["success"], r2
    spread = r2["execution"].get("wave_spread")
    assert spread and spread["targets"] == [foe_a.name, foe_b.name]
    dealt = sum(e.get("actual_damage", 0) for e in r2["execution"]["effects"]
                if e.get("type") == "damage")
    assert dealt == 4, "总数值不变：4点伤害平分给两个目标（杀伐2→X²=4）"
    assert foe_a.current_hp == 98 and foe_b.current_hp == 98


def test_boba_boundary_and_invalid_submissions():
    """边界/错误：X 为负数/非整数非法（X=0 自 2026-08-30 起合法＝拒绝发动）；
    目标不足X个/重复/数量不符时被拒；波及不作用于自身。"""
    engine = _engine("boba_keys")
    p = engine.state.player
    _give(p, "波及")
    m = _monster(engine)
    engine.execute_action("round_start", {})

    # X=0 = 拒绝发动（DM裁定2026-08-30，报告.md 硬伤1b）：合法跳过、不挂波及标记
    r0 = engine.execute_action("use_daowen", {"daowen_name": "波及", "x": 0, "target": m.name})
    assert r0["success"] is True
    assert r0.get("skipped") is True
    assert not getattr(m, "_wave_marks", None)
    # X<1 中真正非法的只剩负数/非整数
    r0b = engine.execute_action("use_daowen", {"daowen_name": "波及", "x": -1, "target": m.name})
    assert r0b["success"] is False
    assert "X必须≥1" in r0b["error"]

    # dodge_targets 数量必须等于 X
    r1 = engine.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2,
        "dodge_targets": [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False}],
    })
    assert r1["success"] is False
    assert "dodge_targets" in r1.get("error", "")

    # 不能标记自身（X=2 档、数量对得上，触发的必须是「非自身」那条校验）
    p_ref = next(ref for ref, e in engine.combat._combat_entity_refs().items() if e is p)
    r2 = engine.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2,
        "dodge_targets": [{"target_ref": p_ref, "dodge": False, "blood_shadow": False},
                          {"target_ref": "enemy:0", "dodge": False, "blood_shadow": False}],
    })
    assert r2["success"] is False

    # 正常发动一次：标记目标（X≥2 → 追加一只怪，两个目标一起标）
    m2 = _monster(engine, "陪标怪")
    r3 = engine.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2,
        "dodge_targets": [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
                          {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False}],
    })
    assert r3["success"], r3
    assert m.has_status("波及") and m2.has_status("波及")



def test_jiahai_status_consumable_guzhi_and_anger_consumable():
    """灼罪钉施加固定加害状态；固执单次掉1；怒火针使法力消耗减半。

    2026-10-11 用户令：【借力】已删除，原「借力2 → _jieli_boost +20%」段落一并移除。
    """
    engine = _engine("hooks")
    assert "加害" not in DaoWenEngine._registry
    assert "愤怒" not in DaoWenEngine._registry
    from engine.rule_engine import DAOWEN_RULES, STATUS_DAMAGE_RULES
    assert "加害" not in DAOWEN_RULES
    assert any(rule.status_name == "加害" for rule in STATUS_DAMAGE_RULES)
    p = engine.state.player
    m = _monster(engine, ap=10)
    engine.execute_action("round_start", {})

    item = Consumable(name="灼罪钉", effect="使目标每次受到伤害+1，持续2回合",
                       current_uses=1, max_uses=1)
    engine.state.consumables.append(item)
    enemy_ref = next(ref for ref, entity in engine.combat._combat_entity_refs().items()
                     if entity is m)
    try:
        used = engine.execute_action("consume_item", {
            "name": "灼罪钉", "target_ref": enemy_ref,
        })
        assert used["success"], used
        assert item.current_uses == 0
        assert m.has_status("加害")
        d = engine.combat._apply_hostile_damage(m, 10)
        assert d["actual_damage"] == 11
    finally:
        engine.state.consumables.remove(item)

    p.current_hp = 50
    pill = Consumable("定命丸", "每次失去生命最多1，持续1回合", 2, 2)
    engine.state.consumables.append(pill)
    assert engine.execute_action("consume_item", {"name": "定命丸"})["success"]
    assert pill.current_uses == 1
    assert p.has_status("固执")
    d2 = p.take_damage(20)
    assert d2["actual_damage"] == 1
    assert p.current_hp == 49
    # 2026-10-11 用户令：固执简化为「你每次失去的生命不会超过1」，代价路径同样封顶。
    # 旧口径下这一击会全额落地 8 点（`damage_type != "代价"` 排除），现已废止。
    d3 = p.take_damage(8, "代价")
    assert d3["actual_damage"] == 1
    assert p.current_hp == 48

    item = Consumable(name="怒火针", effect="使目标法力消耗减半，持续2回合",
                       current_uses=1, max_uses=1)
    engine.state.consumables.append(item)
    player_ref = next(ref for ref, entity in engine.combat._combat_entity_refs().items()
                      if entity is p)
    try:
        used = engine.execute_action("consume_item", {
            "name": "怒火针", "target_ref": player_ref,
        })
        assert used["success"], used
        assert item.current_uses == 0
        assert p.has_status("愤怒")
        mana = p.current_mana
        assert p.spend_mana(10) is True
        assert p.current_mana == mana - 5
    finally:
        engine.state.consumables.remove(item)



def test_huaxiang_dingxing_wushen():
    """定型挡弱化。

    2026-10-03：【滑翔/坠落】删除，原「滑翔视同飞行、坠落落地并减半」段落一并移除。
    2026-10-11 用户令：【无神】删除，原「无神使敌方轮回者的杀伐改打自己」段落一并移除
    （引擎里的强制改目标分支同批删除，见 `engine/combat_parts/spells.py`、`api.py`）。
    """
    engine = _engine("ctrl")
    p = engine.state.player
    for n in ("定型", "弱化"):
        _give(p, n)
    m = _monster(engine, ap=8)
    engine.execute_action("round_start", {})

    m.attack_power = 10
    engine.execute_action("use_daowen", {"daowen_name": "定型", "x": 1, "target": m.name})
    engine.execute_action("use_daowen", {"daowen_name": "弱化", "x": 3, "target": m.name})
    assert m.attack_power == 10





def test_damage_needle_is_forged_from_dragon_valley_event():
    """熔炉余火真实支付流血10并发放灼罪钉。"""
    engine = _engine("damage_needle_forge")
    engine.state.phase = "pre_battle"
    engine.state.current_region = "龙心谷"
    engine.event_pool.current = "熔炉余火"
    hp_before = engine.state.player.current_hp

    result = engine.execute_action("resolve_event", {
        "event": "熔炉余火", "option_id": 3,
    })
    assert result["success"], result
    assert engine.state.player.current_hp == hp_before - 10
    item = next(c for c in engine.state.consumables if c.name == "灼罪钉")
    assert item.current_uses == item.max_uses == 1


def test_anger_needle_is_purchased_from_black_market_event():
    """黑市事件真实扣除12碎片并发放固定耐久的怒火针。"""
    engine = _engine("anger_needle_purchase")
    engine.state.phase = "pre_battle"
    engine.state.current_region = "罪孽都市"
    engine.state.shards = 20
    engine.event_pool.current = "黑市军火贩"

    result = engine.execute_action("resolve_event", {
        "event": "黑市军火贩", "option_id": 3,
    })
    assert result["success"], result
    assert engine.state.shards == 8
    item = next(c for c in engine.state.consumables if c.name == "怒火针")
    assert item.current_uses == item.max_uses == 1


def test_retired_healing_runes_are_not_registered():
    """边界：自愈与滋养已删除，不得通过旧注册表恢复。"""
    DaoWenEngine.register_all()
    assert "自愈" not in DaoWenEngine._registry
    assert "滋养" not in DaoWenEngine._registry
    assert "眩晕" not in DaoWenEngine._registry


def test_relicized_trigger_daowen_registry_and_flight_is_not_a_daowen():
    """已遗物化触发型道纹不可再施放；【飞行】是状态，不出现在道纹注册表。"""
    retired = {
        "逆鳞", "伤痕", "寄生", "加速", "急速", "洞察", "无力",
        "冥气", "退化", "畸变", "缄默", "飞行",
    }
    assert retired.isdisjoint(DaoWenEngine._registry)

    # 2026-10-25 用户令：飞行是状态，不由【变形】凭空附带（旧实现是自造机制，已删）。
    engine = _engine("bone_angel_no_flight")
    bone_angel = _monster(engine, name="骨天使", hp=30, atk=7, ap=3)
    _give(bone_angel, "变形")
    calc = DaoWenEngine.resolve("变形", 1, target=bone_angel, caster=bone_angel)
    engine.combat.apply_daowen_effect("变形", calc, bone_angel, bone_angel)
    assert not bone_angel.has_status("飞行"), "【变形】不得附带飞行状态"


def test_flight_rule_line_matches_the_engine():
    """规则正文的飞行条文与引擎拦目标的口径必须一致。

    2026-10-25 用户令：条文只保留这一句「飞行X：非飞行者无法把飞行者选为[目标]，持续X回合。」
    """
    rules = Path("规则正文.md").read_text(encoding="utf-8")
    section = rules.split("### 战斗行动", 1)[1].split("\n### ", 1)[0]
    assert "飞行X：非飞行者无法把飞行者选为[目标]，持续X回合。" in section
    assert "飞行" not in DaoWenEngine._registry

    engine = _engine("flight_targeting")
    p = engine.state.player
    m = _monster(engine, ap=8)
    p.is_flying = True
    assert engine.combat.is_targetable(p, m) is True, "飞行者可以选地面目标"
    assert engine.combat.is_targetable(m, p) is False, "非飞行者不能选飞行目标"


def test_zengzhi_grants_temporary_infinite_blood_limit_and_bypasses_cancer():
    """增殖X：支付衰老X，目标血限暂时视为无限；有限回复不触发癌变。"""
    engine = _engine("zengzhi_infinite_blood")
    p = engine.state.player
    _give(p, "增殖")
    target = _monster(engine, "增殖靶怪", hp=100)
    engine.execute_action("round_start", {})

    result = engine.execute_action("use_daowen", {
        "daowen_name": "增殖", "x": 1, "target": target.name,
    })
    assert result["success"], result
    assert target.has_status("无限血限")
    assert target.to_dict()["blood_limit"] == "∞"
    assert engine.combat.cancer_threshold_of(target) == 0

    heal = engine.state.apply_heal(target, 300)
    assert heal["hp_after"] == 400
    assert target.total_healed == 300
    assert engine.combat.check_cancer(target) is None
    assert "癌变（300/∞）" in target.lethal_progress()

    # 回终递减持续时间；到期时撤销无限血限，并将生命封顶到基础血限。
    engine.combat.round_end()
    assert not target.has_status("无限血限")
    assert target.current_hp == target.blood_limit == 100


def test_chengni_advances_target_rounds_without_advancing_world_time():
    """大梦推进目标经历的回合，不推进世界回合；状态与凡庸按该回合结算。"""
    engine = _engine("ningzhi_real_round")
    _give(engine.state.player, "大梦")
    target = _monster(engine, "大梦靶怪", hp=100)
    target.add_status(StatusEffect(name="测试增益", value=1, remaining_rounds=2, source="test"))
    target.actions_used_this_round = 1
    target.damage_dealt_this_round = 1
    engine.execute_action("round_start", {})
    before_round = engine.state.current_round
    result = engine.execute_action("use_daowen", {
        "daowen_name": "大梦", "x": 1, "target": target.name,
        "dodge": False, "blood_shadow": False,
    })
    assert result["success"], result
    assert engine.state.current_round == before_round
    assert target.get_status_effects("测试增益")[0].remaining_rounds == 1
    assert target.no_action_rounds == 1
    assert target.no_damage_rounds == 1
    assert any(e.get("type") == "extra_target_round" for e in result["execution"]["effects"])


def test_chengni_triggers_nightwatch_per_extra_round_using_world_round():
    """大梦每让角色经历一个回合就触发守夜灯，但收益始终按世界回合数计算。"""
    engine = _engine("chengni_nightwatch")
    target = Entity(
        name="持灯轮回者", entity_type="轮回者",
        blood_limit=100, current_hp=100,
        mana_limit=100, current_mana=0,
        speed_limit=3, current_speed=3,
        relics=[],
    )
    engine.state.enemies.append(target)
    engine.state.opponent_relics.append(
        Relic("守夜灯", "每经历一个回合获得当前世界回合数的法力"))
    engine.state.current_round = 10
    target.add_status(StatusEffect(name="测试减益", value=1, remaining_rounds=3, source="test"))
    engine.state.player.add_status(
        StatusEffect(name="玩家状态", value=1, remaining_rounds=3, source="test"))

    effects = engine.combat.advance_target_rounds(target, 3, source="大梦")

    assert engine.state.current_round == 10
    assert target.current_mana == 30
    assert not target.has_status("测试减益")
    assert engine.state.player.get_status_effects("玩家状态")[0].remaining_rounds == 3
    assert target.actions_used_this_round == 0
    assert len([e for e in effects if isinstance(e, dict) and e.get("type") == "extra_target_round"]) == 3


def test_jinghua_rejects_mixed_cost_layers():
    """净化只能选择一种数值类代价，不能混选或拆分X。"""
    engine = _engine("jinghua_cost_choices")
    _give(engine.state.player, "净化")
    target = _monster(engine, "净化靶怪", hp=100)
    target.blood_limit = 100
    target.mutation_count = 4
    engine.execute_action("round_start", {})
    result = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 3, "target": target.name,
        "cost_choices": [{"type": "衰老", "layers": 1}, {"type": "异变", "layers": 2}],
        "dodge": False, "blood_shadow": False,
    })
    assert not result["success"], result
    assert target.blood_limit == 100
    assert target.mutation_count == 4

