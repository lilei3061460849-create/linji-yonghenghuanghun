"""手操 2026081311 探灯贾凡复现的引擎回归锁。

1. 同名重复怪 + 封印尸体：declare_evolution 必须命中活着的那只
2. 「拒绝改造」不是拒绝类：流血/碎片照常，无所求不触发
3. 备用血泵 / 急救箱 必须走 Entity.heal（癌变记账 + 战终回吐）
4. 高爆手雷减本回合攻击次数（每出手少打一下），不改面板、不减出手数

每条覆盖正常 / 边界 / 错误输入。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, Relic, StatusEffect
from tests.monster_phase_support import resolve_monster_phase


def _engine(suffix: str, region: str = "扭曲都市") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_hp1311_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "探灯贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    return engine


def _plight(name: str, hp=30, limit=120) -> Entity:
    return Entity(name=name, entity_type="怪物", blood_limit=limit, current_hp=hp,
                  attack_count=2, attack_power=1)


# ========================================================================
# 1. 进化撞封印尸体
# ========================================================================

def test_evolution_skips_sealed_corpse_and_hits_living_namesake():
    """正常路径：先封印同名尸体，活着的同名困境怪仍可进化。"""
    engine = _engine("evo_happy")
    dead = _plight("孢子母体", hp=0, limit=252)
    dead.is_alive = False
    dead.removed_without_kill = True
    live = _plight("孢子母体", hp=30, limit=252)
    engine.state.enemies = [dead, live]
    engine.state.phase = "in_combat"
    r = engine.execute_action("declare_evolution", {
        "monster": "孢子母体", "daowen": "杀伐", "x": 1,
    })
    assert r["success"], r
    assert "杀伐" in live.dao_wen
    assert "杀伐" not in dead.dao_wen
    assert live.mutation_count == 5


def test_evolution_two_living_namesakes_picks_first_alive():
    """边界：两只都活着的同名怪，进化第一只活着的，第二只不动。"""
    engine = _engine("evo_bound")
    a = _plight("血肉巨囊", hp=20, limit=258)
    b = _plight("血肉巨囊", hp=20, limit=258)
    engine.state.enemies = [a, b]
    engine.state.phase = "in_combat"
    r = engine.execute_action("declare_evolution", {
        "monster": "血肉巨囊", "daowen": "杀伐", "x": 1,
    })
    assert r["success"], r
    assert "杀伐" in a.dao_wen
    assert "杀伐" not in b.dao_wen


def test_evolution_only_corpses_rejected():
    """错误输入：场上只剩同名尸体，拒绝且不改状态。"""
    engine = _engine("evo_invalid")
    dead = _plight("孢子母体", hp=0, limit=252)
    dead.is_alive = False
    dead.removed_without_kill = True
    engine.state.enemies = [dead]
    engine.state.phase = "in_combat"
    r = engine.execute_action("declare_evolution", {
        "monster": "孢子母体", "daowen": "杀伐", "x": 1,
    })
    assert r["success"] is False
    assert "找不到存活" in r["error"]
    assert "杀伐" not in dead.dao_wen


# ========================================================================
# 2. 拒绝改造 ≠ 拒绝类
# ========================================================================

def test_refuse_gaizao_applies_bleed_and_shards_without_wushi():
    """正常路径：医生「拒绝改造」失去生命6（现行代价名：生命）、+8碎片，不记无事发生。"""
    engine = _engine("doc_happy")
    p = engine.state.player
    p.current_hp = 56
    engine.state.shards = 97
    engine.event_pool.current = "医生"
    r = engine.execute_action("resolve_event", {"event": "医生", "option_id": 2})
    assert r["success"], r
    applied = r["result"]["applied"]
    assert "生命6" in applied
    assert any("碎片" in a for a in applied)
    assert "无事发生" not in applied
    assert p.current_hp == 50
    assert engine.state.shards == 105


def _resolve_reject(engine, event, option_id):
    """结算一次真拒绝；扭曲都市事件后会附赠一次【发现】，需先选掉才能继续。"""
    engine.event_pool.current = event
    r = engine.execute_action("resolve_event", {"event": event, "option_id": option_id})
    assert r["success"], r
    bonus = r.get("result", {}).get("附赠发现")
    if bonus and bonus.get("等待选择"):
        picked = engine.execute_action(
            "choose_discovered_item", {"item_name": bonus["候选"][0]})
        assert picked["success"], picked
    return r


def test_true_refuse_still_wushi_and_wusuoqiu():
    """边界：真拒绝「拒绝：无事发生」仍记无事发生；持无所求则+1属性点入池。"""
    engine = _engine("doc_bound")
    engine.state.relics.append(Relic(name="无所求", effect=""))
    pool = engine.state.attribute_points
    engine.event_pool.current = "祭坛"
    r = engine.execute_action("resolve_event", {"event": "祭坛", "option_id": 2})
    assert r["success"], r
    assert "无事发生" in r["result"]["applied"]
    assert any("无所求" in a for a in r["result"]["applied"])
    assert engine.state.attribute_points == pool + 1


def test_wusuoqiu_points_redeem_at_standard_rate():
    """正常路径：无所求给的是属性点，按正文 2点=1速限=1法限 兑换。

    2026-09-16 裁定（清单 A1）：旧实现按"1属性点=1速限=2法限"直接改面板，
    与正文「2属性点=1[速限]=1[法限]」相悖且速限/法限不等价。现改为入池，
    攒够 2 点兑 1 速限或 1 法限。
    """
    engine = _engine("wusuoqiu_redeem")
    engine.state.relics.append(Relic(name="无所求", effect=""))
    p = engine.state.player
    ml, sp = p.mana_limit, p.speed_limit
    r = _resolve_reject(engine, "祭坛", 2)
    assert any("无所求" in a for a in r["result"]["applied"])
    assert engine.state.attribute_points == 1, "第一次拒绝只给1点，不足以兑换"
    assert p.mana_limit == ml and p.speed_limit == sp, "1点属性点换不到任何面板"
    # 再拒绝一次（遗忘书屋）攒够 2 点，按正文兑 1 点法限
    _resolve_reject(engine, "遗忘书屋", 4)
    assert engine.state.attribute_points == 2
    rr = engine.execute_action("redeem_attribute_points", {"allocations": {"mana_points": 2}})
    assert rr["success"], rr
    assert p.mana_limit == ml + 1, f"2点应兑1法限，实+{p.mana_limit - ml}"


def test_wusuoqiu_does_not_fire_on_non_reject_option():
    """边界：非拒绝类选项不给属性点（无所求只认真拒绝）。"""
    engine = _engine("wusuoqiu_nonreject")
    engine.state.relics.append(Relic(name="无所求", effect=""))
    pool = engine.state.attribute_points
    engine.state.shards = 100
    engine.event_pool.current = "祭坛"
    r = engine.execute_action("resolve_event", {"event": "祭坛", "option_id": 1, "x": 1, "multiplier": 1})  # 投资本金1，倍率1
    assert r["success"], r
    assert engine.state.attribute_points == pool, "非拒绝选项不得给属性点"


def test_wusuoqiu_points_accumulate_in_pool():
    """正常路径：多次真拒绝的属性点累加进池，跨事件保留。"""
    engine = _engine("wusuoqiu_accum")
    engine.state.relics.append(Relic(name="无所求", effect=""))
    base = engine.state.attribute_points
    # 祭坛2＝拒绝：无事发生；遗忘书屋4＝拒绝：无事发生
    for event, oid in (("祭坛", 2), ("遗忘书屋", 4)):
        _resolve_reject(engine, event, oid)
    assert engine.state.attribute_points == base + 2, "两次真拒绝应累计2点属性点"


def test_wusuoqiu_does_not_fire_on_refuse_gaizao():
    """错误对照：持无所求选拒绝改造，不得白给属性点。"""
    engine = _engine("doc_invalid")
    engine.state.relics.append(Relic(name="无所求", effect=""))
    pool = engine.state.attribute_points
    engine.state.shards = 10
    engine.event_pool.current = "医生"
    r = engine.execute_action("resolve_event", {"event": "医生", "option_id": 2})
    assert r["success"], r
    assert engine.state.attribute_points == pool, "拒绝改造不是真拒绝，不给属性点"
    assert not any("无所求" in a for a in r["result"]["applied"])


# ========================================================================
# 3. 工具回血走 heal()
# ========================================================================

def test_blood_pump_goes_through_heal():
    """正常路径：备用血泵 +20 计入 total_healed / healed_this_battle。"""
    engine = _engine("pump_happy")
    p = engine.state.player
    p.current_hp = 30
    engine.state.consumables.append(Consumable(
        name="备用血泵", effect="使自身获得20点［回复］", current_uses=3, max_uses=3))
    r = engine.execute_action("consume_item", {"name": "备用血泵"})
    assert r["success"], r
    assert p.current_hp == 50
    assert r["result"]["healed"] == 20
    assert p.healed_this_battle == 20
    assert p.total_healed >= 20


def test_medkit_overheal_counts_double_for_cancer():
    """边界：满血急救箱实回复0，过量25按原值计入累计恢复（双倍机制已删，DM裁定2026-08-18）。"""
    engine = _engine("kit_bound")
    p = engine.state.player
    assert p.current_hp == p.blood_limit == 66   # 11血点×6（DM裁定 2026-09-10）
    engine.state.consumables.append(Consumable(
        name="急救箱", effect="使自身获得[回复25]", current_uses=2, max_uses=2))
    r = engine.execute_action("consume_item", {"name": "急救箱"})
    assert r["success"]
    assert p.current_hp == 66   # 满血时实回复0，当前生命仍等于血限66
    assert r["result"]["healed"] == 0
    assert p.total_healed == 25  # 过量按原值计入（双倍机制已删）


def test_missing_pump_does_not_heal():
    """错误输入：没有血泵时拒绝，生命/累计恢复不变。"""
    engine = _engine("pump_invalid")
    p = engine.state.player
    p.current_hp = 30
    r = engine.execute_action("consume_item", {"name": "备用血泵"})
    assert r["success"] is False
    assert p.current_hp == 30
    assert p.total_healed == 0


# ========================================================================
# 4. 高爆手雷减攻击次数
# ========================================================================

def test_grenade_cuts_hits_not_actions():
    """正常路径：3×5 怪挨手雷后本回合只打 2 下，出手数仍为 1。"""
    engine = _engine("nade_happy")
    m = Entity(name="畸变行者", entity_type="怪物", blood_limit=210, current_hp=210,
               attack_count=3, attack_power=8)
    engine.state.enemies = [m]
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()
    engine.state.consumables.append(Consumable(
        name="高爆手雷", effect="造成15点伤害，并使其本回合攻击次数-1",
        current_uses=2, max_uses=2))
    r = engine.execute_action("consume_item", {"name": "高爆手雷", "target": "畸变行者"})
    assert r["success"], r
    assert m.current_hp == 195
    assert m.attack_count == 3
    assert engine.combat._monster_attack_actions(m, set()) == 1
    details = resolve_monster_phase(engine.combat, {m.name: None})
    hits = [d for d in details if "damage_dealt" in d or "dodge_success" in d]
    assert len(hits) == 2, f"应打2下，实{len(hits)} {details}"
    assert all(d.get("hit_total") == 2 for d in hits)


def test_grenade_can_zero_hits_without_sculpting():
    """边界：1次攻击的怪挨手雷后本回合0下，攻击次数面板仍为1，不雕塑。"""
    engine = _engine("nade_bound")
    m = Entity(name="眼树", entity_type="怪物", blood_limit=198, current_hp=198,
               attack_count=1, attack_power=13)
    engine.state.enemies = [m]
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()
    engine.state.consumables.append(Consumable(
        name="高爆手雷", effect="造成15点伤害，并使其本回合攻击次数-1",
        current_uses=2, max_uses=2))
    engine.execute_action("consume_item", {"name": "高爆手雷", "target": "眼树"})
    details = resolve_monster_phase(engine.combat, {m.name: None})
    hits = [d for d in details if "damage_dealt" in d or "dodge_success" in d]
    assert hits == []
    assert m.attack_count == 1
    assert m.is_alive and not m.is_sculptured


def test_grenade_missing_target_refunds_durability():
    """错误输入：找不到目标不扣耐久。"""
    engine = _engine("nade_invalid")
    engine.state.enemies = []
    item = Consumable(name="高爆手雷", effect="...", current_uses=2, max_uses=2)
    engine.state.consumables.append(item)
    r = engine.execute_action("consume_item", {"name": "高爆手雷", "target": "没有这只怪"})
    assert r["success"] is False
    assert item.current_uses == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
