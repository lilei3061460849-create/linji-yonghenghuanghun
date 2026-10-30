"""
pytest 风格测试 - 真龙之心（龙心谷终音法器，代价累积 + 8 种龙族遗物）

2026-10-23 用户令：废除「龙性」货币。玩家**实际支付**的数值型【代价】
（流血/衰老/枯竭/萎缩/疲惫）自动记 1:1 入账，每满 12 点换 1 件龙族遗物。
7 件龙族遗物：龙族血脉/龙威/龙族利爪/龙息/震岳龙躯/吞骸龙胃/断尾求生（全部被动、无 X、有触发时机），
外加消耗品【烬翼】（5/5，主动飞行；任意怪物命零时耐久+1）。

覆盖范围：unlock_dragon_trait 的门槛校验 + 8 件龙族遗物各自的被动效果。
运行方式：
    python -m pytest tests/test_dragon_traits.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.attack_support import resolve_attack as resolve_player_attack
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance
from tests.monster_phase_support import resolve_monster_phase


def _new_engine(region="龙心谷", name="老张", speed=8, mana=6, dbsuffix="a"):
    engine = GameEngine(db_path=f"data/test_dragon_traits_{dbsuffix}.db", rng_seed=1,
                         sealed_candidate_path=f"data/test_dragon_traits_{dbsuffix}_sealed.json")
    blood = 25 - speed - mana
    engine.execute_action("setup_attributes",
                           {"blood_points": blood, "speed_points": speed, "mana_points": mana, "name": name})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    engine.state.energy = 0
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _with_heart(engine, points=100):
    engine.state.artifacts_owned.append("真龙之心")
    engine.state.dragon_cost_points = points
    return engine


def _resolve_full_attack(engine, attacker_name, target, n_hits=1):
    return resolve_player_attack(engine, attacker_name, [0] * n_hits)


# ========================================================================
# 代价累积：实际支付的数值型代价自动入账 / unlock_dragon_trait
# ========================================================================

def test_actual_numeric_cost_accumulates_with_heart():
    """正常路径：真正支付 1 点数值型代价 = 1 点代价累积，不需要任何兑换步骤。"""
    from engine.models import Consumable
    engine = _new_engine(dbsuffix="accrue")
    player = engine.state.player
    _with_heart(engine, points=0)
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})

    engine.combat.pay_numeric_cost(player, "流血", 4)
    assert engine.state.dragon_cost_points == 4
    engine.combat.pay_numeric_cost(player, "衰老", 3)
    assert engine.state.dragon_cost_points == 7


def test_cost_accumulation_requires_holding_artifact():
    """边界：没有真龙之心时，支付代价不产生任何累积。"""
    engine = _new_engine(dbsuffix="accrue_off")
    player = engine.state.player
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    engine.combat.pay_numeric_cost(player, "流血", 5)
    assert engine.state.dragon_cost_points == 0


def test_unlock_dragon_trait_requires_12_points_and_rejects_duplicates():
    """边界+错误输入：满12点消耗12点解锁；不足/未知/重复均应拒绝。"""
    engine = _new_engine(dbsuffix="unlock")
    _with_heart(engine, points=11)
    r_low = engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    assert r_low["success"] is False
    assert "代价累积不足" in r_low["error"]

    engine.state.dragon_cost_points = 12
    r_ok = engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    assert r_ok["success"] is True
    assert engine.state.dragon_cost_points == 0
    assert "龙威" in engine.state.dragon_traits

    assert engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})["success"] is False
    assert engine.execute_action("unlock_dragon_trait", {"trait": "不存在"})["success"] is False


# ========================================================================
# 1. 龙族血脉
# ========================================================================

def test_dragon_bloodline_instakills_monsters_and_doubles_nonmonster_damage():
    """正常路径：对怪物造成伤害后直接命零；对非怪物造成伤害翻倍"""
    engine = _new_engine(dbsuffix="bloodline")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙族血脉"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="怪物", entity_type="怪物", blood_limit=999999, current_hp=999999)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写面板无效
    player.current_speed = 1
    player.current_mana = 5
    _resolve_full_attack(engine, player.name, monster)
    assert monster.is_alive is False, "怪物应被直接命零"

    non_monster = Entity(name="敌方轮回者", entity_type="轮回者", blood_limit=100, current_hp=100)
    dmg = engine.combat.resolve_attack(player, non_monster, is_must_hit=True)
    assert dmg["damage_dealt"] == 10, "龙族血脉翻倍：攻力(当前法力)5×2"


# ========================================================================
# 2. 龙威（无代码变更的既有行为断言，避免今后误改）
# ========================================================================

def test_dragon_might_monsters_always_target_player():
    """回归性断言：怪物阶段固定只攻击玩家本人，因此龙威("敌方必须优先选择自身为目标")天然恒定满足"""
    engine = _new_engine(dbsuffix="might")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="怪物", entity_type="怪物", blood_limit=30, current_hp=30, attack_power=1, attack_count=1)
    engine.state.enemies.append(monster)
    engine.state.friends.append(Entity(name="队友", entity_type="朋友", blood_limit=30, current_hp=30, is_deployed=True))
    engine.execute_action("round_start", {})
    results = resolve_monster_phase(engine.combat, {"怪物": None})
    targets = {r["target"] for r in results if "target" in r}
    assert targets == {player.name}


# ========================================================================
# 3. 龙族利爪
# ========================================================================

def test_dragon_claw_doubles_attack_power():
    """正常路径（2026-09-17 用户令改版）：攻击力 = 当前法力×2。

    旧版「初始3点攻击次数与1点攻击力；每完成一次行动后，攻击次数+1、攻击力+2」
    写的是遗留字段 attack_count / attack_power。属性模型统一后
    （攻击力=当前法力、攻击次数=当前速度），而 models.py 的写穿对轮回者不生效，
    旧效果**对轮回者完全无效**——实测遗留字段 6→7 / 20→22，真实攻次与攻力恒定
    6 / 20。故改为被动倍率，走状态层。
    """
    engine = _new_engine(dbsuffix="claw")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙族利爪"})

    engine.execute_action("battle_start", {})
    assert player.has_status("龙族利爪"), "战始应挂上【龙族利爪】状态"

    player.current_mana = 10
    assert player.effective_attack_power() == 20, \
        f"法力10 → 攻力应20（×2），实{player.effective_attack_power()}"
    player.current_mana = 3
    assert player.effective_attack_power() == 6, \
        f"法力3 → 攻力应6（×2，实时跟随），实{player.effective_attack_power()}"
    # 对照：未持有时攻力 = 当前法力
    bare = _new_engine(dbsuffix="clawbare")
    bare.state.player.current_mana = 10
    assert bare.state.player.effective_attack_power() == 10, "未持有龙族利爪时攻力应等于当前法力"


# ========================================================================
# 4. 龙息
# ========================================================================

def test_dragon_breath_deals_10_times_round_damage_before_each_monster_acts():
    """正常路径：所有敌方目标行动前，受到10×当前回合数的必中伤害"""
    engine = _new_engine(dbsuffix="breath")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="怪物", entity_type="怪物", blood_limit=100, current_hp=100, attack_power=1, attack_count=1)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})  # current_round -> 1
    results = resolve_monster_phase(engine.combat, {"怪物": None})
    breath_entries = [r for r in results if r.get("dragon_breath")]
    assert breath_entries and breath_entries[0]["dragon_breath"] == 10
    assert monster.current_hp == 90


def test_dragon_breath_can_kill_monster_before_it_acts():
    """边界：龙息伤害足以命零怪物时，该怪物本回合跳过后续行动"""
    engine = _new_engine(dbsuffix="breath_kill")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    weak = Entity(name="弱怪", entity_type="怪物", blood_limit=5, current_hp=5, attack_power=1, attack_count=1)
    engine.state.enemies.append(weak)
    engine.execute_action("round_start", {})
    results = resolve_monster_phase(engine.combat, {"弱怪": None})
    assert weak.is_alive is False
    assert not any(r.get("attacker") == "弱怪" for r in results), "已被龙息命零的怪物不应再发动攻击"


# ========================================================================
# 5. 震岳龙躯
# ========================================================================

def test_dragon_body_passively_caps_single_hit_at_10():
    """正常路径（重做后）：持有震岳龙躯即常驻生效，单次超过10点的伤害只落10点。"""
    engine = _new_engine(dbsuffix="body")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "震岳龙躯"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})

    engine.state.enemies.clear()
    heavy = Entity(name="重击怪", entity_type="怪物", blood_limit=50, current_hp=50,
                   attack_power=999, attack_count=1)
    engine.state.enemies.append(heavy)
    hp_before = player.current_hp
    result = engine.combat.resolve_attack(heavy, player, is_must_hit=True)
    assert result["damage_dealt"] == 10
    assert player.current_hp == hp_before - 10


def test_dragon_body_boundary_leaves_small_hits_untouched():
    """边界：不超过10点的单次伤害原样落地，不会被抬到10。"""
    engine = _new_engine(dbsuffix="body_small")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "震岳龙躯"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})

    engine.state.enemies.clear()
    light = Entity(name="轻击怪", entity_type="怪物", blood_limit=50, current_hp=50,
                   attack_power=7, attack_count=1)
    engine.state.enemies.append(light)
    hp_before = player.current_hp
    result = engine.combat.resolve_attack(light, player, is_must_hit=True)
    assert result["damage_dealt"] == 7
    assert player.current_hp == hp_before - 7


# ========================================================================
# 6. 吞骸龙胃
# ========================================================================

def test_dragon_stomach_devour_heals_and_boosts_dragon_heart():
    """正常路径：吞噬已命零的怪物，回复12，并可指定一枚龙心使其耐久+6"""
    from engine.models import Consumable
    engine = _new_engine(dbsuffix="devour")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "吞骸龙胃"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    engine.state.enemies.clear()
    dead = Entity(name="尸体", entity_type="怪物", blood_limit=30, current_hp=0, is_alive=False)
    engine.state.enemies.append(dead)
    engine.state.consumables.append(
        Consumable(name="衰老龙心", effect="", current_uses=4, max_uses=4, kind="dragon_heart",
                   dragon_heart_type="衰老"))
    player.current_hp = 10
    r = engine.execute_action("devour_monster", {"monster_ref": "enemy:0", "dragon_heart": "衰老龙心"})
    assert r["success"] is True
    assert player.current_hp == 22
    heart = next(c for c in engine.state.consumables if c.name == "衰老龙心")
    assert heart.current_uses == 10
    assert heart.max_uses == 10


def test_dragon_stomach_error_target_still_alive():
    """错误输入：目标怪物尚未命零时拒绝"""
    engine = _new_engine(dbsuffix="devour_err")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "吞骸龙胃"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    alive = Entity(name="活怪", entity_type="怪物", blood_limit=30, current_hp=30)
    engine.state.enemies.append(alive)
    r = engine.execute_action("devour_monster", {"monster_ref": "enemy:0", "dragon_heart": ""})
    assert r["success"] is False


# ========================================================================
# 7. 断尾求生
# ========================================================================

def test_tail_sacrifice_removes_declared_trait_to_negate_lethal_damage():
    """正常路径：预声明后，即将命零的伤害改为移除该遗物抵消"""
    engine = _new_engine(dbsuffix="tail")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "断尾求生"})
    engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    engine.execute_action("declare_tail_sacrifice", {"trait": "龙威"})
    player.current_hp = 5
    result = engine.combat._apply_hostile_damage(player, 9999, "普通")
    assert result.get("tail_sacrificed") == "龙威"
    assert player.current_hp == 5
    assert player.is_alive is True
    assert "龙威" not in engine.state.dragon_traits
    assert engine.state.dragon_tail_sacrifice_declared == ""


def test_tail_sacrifice_error_declare_self_or_unowned_trait():
    """错误输入：不能声明牺牲自身(断尾求生)，也不能声明未持有的其他遗物"""
    engine = _new_engine(dbsuffix="tail_err")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "断尾求生"})
    engine.state.phase = "in_combat"
    r_self = engine.execute_action("declare_tail_sacrifice", {"trait": "断尾求生"})
    assert r_self["success"] is False
    r_unowned = engine.execute_action("declare_tail_sacrifice", {"trait": "龙威"})
    assert r_unowned["success"] is False


# ========================================================================
# 8. 烬翼
# ========================================================================

def test_ember_wing_unlock_grants_consumable_not_relic():
    """正常路径（2026-10-23）：烬翼改为消耗品 3/3，兑换仍花 12 点，不占龙族遗物位。"""
    engine = _new_engine(dbsuffix="wings")
    _with_heart(engine, points=12)
    r = engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})
    assert r["success"], r.get("error")
    wing = next((c for c in engine.state.consumables if c.name == "烬翼"), None)
    assert wing is not None
    assert (wing.current_uses, wing.max_uses) == (3, 3)
    assert "烬翼" not in engine.state.dragon_traits
    assert engine.state.dragon_cost_points == 0


def test_ember_wing_active_use_grants_flight():
    """正常路径：主动使用获得【飞行】1回合，耐久 3→2。"""
    engine = _new_engine(dbsuffix="wings_use")
    _with_heart(engine, points=12)
    engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    player = engine.state.player
    r = engine.execute_action("consume_item", {"name": "烬翼", "mode": "fly"})
    assert r["success"], r
    assert player.get_status_value("飞行") == 1
    assert r["result"]["uses_remaining"] == 2


def test_ember_wing_recharges_on_any_monster_death():
    """正常路径：任意怪物［命零］时耐久+1。"""
    engine = _new_engine(dbsuffix="wings_recharge")
    _with_heart(engine, points=12)
    engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    wing = next(c for c in engine.state.consumables if c.name == "烬翼")
    wing.current_uses = 2
    monster = Entity(name="杂兵", entity_type="怪物", blood_limit=20, current_hp=20)
    engine.state.enemies.append(monster)
    monster.current_hp = 0
    engine.combat._check_hp_zero_death(monster, None)
    assert wing.current_uses == 3


def test_ember_wing_recharge_ignores_non_monsters_and_caps():
    """边界：非怪物命零不加耐久；耐久已满时不再增长。"""
    engine = _new_engine(dbsuffix="wings_cap")
    _with_heart(engine, points=12)
    engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    wing = next(c for c in engine.state.consumables if c.name == "烬翼")
    wing.current_uses = wing.max_uses
    friend = Entity(name="朋友甲", entity_type="朋友", blood_limit=20, current_hp=20)
    engine.state.friends.append(friend)
    friend.current_hp = 0
    engine.combat._check_hp_zero_death(friend, None)
    assert wing.current_uses == wing.max_uses
    monster = Entity(name="杂兵二", entity_type="怪物", blood_limit=20, current_hp=20)
    engine.state.enemies.append(monster)
    monster.current_hp = 0
    engine.combat._check_hp_zero_death(monster, None)
    assert wing.current_uses == wing.max_uses, "满耐久不得溢出"


def test_ember_wing_duplicate_unlock_rejected():
    """错误输入：已持有一件烬翼消耗品时不得重复兑换。"""
    engine = _new_engine(dbsuffix="wings_dup")
    _with_heart(engine, points=24)
    assert engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})["success"]
    assert engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})["success"] is False
