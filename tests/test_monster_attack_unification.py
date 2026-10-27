"""怪物攻击并入玩家同一套攻击机制（2026-10-25 用户令）。

统一后的口径：
- 面板同源：击数＝`effective_attack_count()`（＝当前速度），攻击力＝`effective_attack_power()`
  （＝当前法力）；prepare 直接给出 `hit_count` / `damage_budget` / `hits_schema`。
- 逐击契约同名：每击显式提交 `target_ref` / `damage` / `dodge` / `parry` / `blood_shadow`，
  与玩家侧 `prepare_attack` → `resolve_attack` 完全一致。
- 怪物也会招架：招架是受击方的回合外行动，与攻击者类型无关——玩家打怪物时，
  怪物同样可以逐击招架，真刀免疫并返还速度，空刀不返还。
"""
import pytest

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, StatusEffect


def _arena(*, player_speed=4, player_hp=100, monster_speed=3, monster_mana=5,
           monster_hp=60):
    state = GameState()
    state.rng_seed = 1
    state.current_round = 1
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=player_hp,
                    current_hp=player_hp, mana_limit=4, current_mana=4,
                    speed_limit=player_speed, current_speed=player_speed)
    monster = Entity(name="石像鬼", entity_type="怪物", blood_limit=monster_hp,
                     current_hp=monster_hp, mana_limit=monster_mana,
                     current_mana=monster_mana, speed_limit=monster_speed,
                     current_speed=monster_speed)
    state.player = player
    state.enemies = [monster]
    state.phase = "in_combat"
    state.combat_subphase = "player_actions"
    return state, CombatEngine(state, DiceEngine(seed=1)), player, monster


def _hit(ref, *, damage=None, dodge=False, parry=False):
    hit = {"target_ref": ref, "dodge": dodge, "parry": parry,
           "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    if damage is not None:
        hit["damage"] = damage
    return hit


def _submit(combat, hits):
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    return combat.resolve_monster_phase(
        [{"actor_ref": actor["actor_ref"], "daowen": None,
          "attack_actions": [{"hits": hits}]}], prepared=prepared)


# ============================================================================
# 一、正常路径：面板与逐击契约和玩家侧同源同名
# ============================================================================

def test_prepare_exposes_the_same_contract_as_player_attacks():
    """击数＝当前速度、伤害预算＝击数×当前法力，并带与玩家侧同名的 hits_schema。"""
    _, combat, _, monster = _arena(monster_speed=3, monster_mana=5)
    actor = combat.prepare_monster_phase()["actors"][0]
    assert actor["hit_count"] == monster.effective_attack_count() == 3
    assert actor["damage_budget"] == 3 * monster.effective_attack_power() == 15
    assert len(actor["hits_schema"]) == actor["hit_count"]
    for field in ("target_ref", "damage", "dodge", "parry", "blood_shadow"):
        assert field in actor["hits_schema"][0], f"hits_schema 缺字段 {field}"
    target = actor["attack_target_options"][0]
    assert target["can_parry"] is True and target["can_dodge"] is True


def test_monster_allocates_damage_freely_inside_the_budget():
    """逐击伤害由怪物自由分配：把3击的15点集中到第一击。"""
    _, combat, player, _ = _arena(monster_speed=3, monster_mana=5)
    results = _submit(combat, [_hit("player:0", damage=15),
                               _hit("player:0", damage=0),
                               _hit("player:0", damage=0)])
    dealt = [r.get("damage_dealt") for r in results if "damage_dealt" in r]
    assert sum(dealt) == 15
    assert player.current_hp == 85
    assert dealt[0] == 15, "伤害应集中在第一击"


def test_empty_hits_do_not_damage_the_target():
    """边界：整轮空刀（全部 damage=0）合法，且不造成伤害。"""
    _, combat, player, _ = _arena(monster_speed=3, monster_mana=5)
    results = _submit(combat, [_hit("player:0", damage=0) for _ in range(3)])
    assert player.current_hp == 100
    assert all(r.get("damage_dealt", 0) == 0 for r in results if "damage_dealt" in r)


def test_hits_follow_the_live_panel_after_a_shape_change():
    """击数取出手时的当前面板：速度被状态改掉后，提交数必须跟着变。"""
    _, combat, _, monster = _arena(monster_speed=3, monster_mana=5)
    monster.add_status(StatusEffect(name="疲惫", value=1, remaining_rounds=1,
                                    source="测"))
    monster.current_speed -= 1  # 疲惫X：扣自身X点当前速度
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    assert actor["hit_count"] == 2
    out = combat.resolve_monster_phase(
        [{"actor_ref": actor["actor_ref"], "daowen": None,
          "attack_actions": [{"hits": [_hit("player:0") for _ in range(2)]}]}],
        prepared=prepared)
    assert out and len(out) == 2


def test_grenade_reduction_goes_through_the_shared_attack_count():
    """【手雷减攻】减的是统一后的攻击次数（＝当前速度），怪物不再单独读旧面板字段。"""
    _, combat, _, monster = _arena(monster_speed=3, monster_mana=5)
    monster.add_status(StatusEffect(name="手雷减攻", value=1, remaining_rounds=1,
                                    source="高爆手雷"))
    actor = combat.prepare_monster_phase()["actors"][0]
    assert actor["hit_count"] == 2, "手雷减攻应在 effective_attack_count 里生效"


# ============================================================================
# 二、边界：防御走同一套逐击结算
# ============================================================================

def test_player_parry_against_monster_hits_negates_a_real_hit():
    """玩家招架怪物的真刀：免疫该击伤害并返还1点速度。"""
    _, combat, player, _ = _arena(monster_speed=3, monster_mana=5, player_speed=4)
    speed_before = player.current_speed
    results = _submit(combat, [_hit("player:0", damage=5, parry=True),
                               _hit("player:0", damage=0),
                               _hit("player:0", damage=0)])
    parried = results[0]
    assert parried.get("parry_success") is True
    assert parried.get("damage_dealt") == 0
    assert player.current_hp == 100, "招架真刀不应掉血"
    assert player.current_speed == speed_before, "招架真刀应返还速度"


def test_defense_total_is_capped_by_current_speed():
    """错误：一次攻击内同一目标的闪避+招架总数超过其当前速度必须被拒。"""
    _, combat, player, _ = _arena(monster_speed=3, monster_mana=5, player_speed=1)
    hp_before = player.current_hp
    with pytest.raises(ValueError, match="速度不足以完成全部防御"):
        _submit(combat, [_hit("player:0", damage=1, parry=True),
                         _hit("player:0", damage=1, parry=True),
                         _hit("player:0", damage=0)])
    assert player.current_hp == hp_before, "非法提交必须整体回滚，不得留下伤害"


# ============================================================================
# 三、错误路径：逐击契约与玩家侧一致地严格
# ============================================================================

def test_damage_over_budget_is_rejected():
    """错误：分配总伤害超过 damage_budget 必须被拒。"""
    _, combat, _, _ = _arena(monster_speed=2, monster_mana=5)
    with pytest.raises(ValueError, match="超过上限"):
        _submit(combat, [_hit("player:0", damage=6), _hit("player:0", damage=6)])
    # 失败即快照回滚，state 里的实体已被替换为快照副本，必须重新读取
    assert combat.state.player.current_hp == 100


def test_negative_and_bool_damage_are_rejected():
    """错误：damage 必须是非负整数，布尔值不算整数。"""
    for bad in (-1, True):
        _, combat, _, _ = _arena(monster_speed=1, monster_mana=5)
        with pytest.raises(ValueError, match="damage必须是非负整数"):
            _submit(combat, [_hit("player:0", damage=bad)])


def test_one_hit_cannot_dodge_and_parry_together():
    """错误：同一次判定只能选闪避、招架、血影之一。"""
    _, combat, _, _ = _arena(monster_speed=1, monster_mana=5)
    with pytest.raises(ValueError, match="只能选择闪避、招架、血影之一"):
        _submit(combat, [_hit("player:0", damage=5, dodge=True, parry=True)])


def test_wrong_hit_count_is_rejected():
    """错误：每击数量必须等于当前攻击次数。"""
    _, combat, _, _ = _arena(monster_speed=3, monster_mana=5)
    with pytest.raises(ValueError, match="必须提交3次命中选择"):
        _submit(combat, [_hit("player:0"), _hit("player:0")])


# ============================================================================
# 四、怪物也会招架：作为受击方走同一套招架结算
# ============================================================================

def test_monster_can_parry_the_player_attack(tmp_path):
    """玩家打怪物时，怪物同样可以逐击招架（招架与攻击者类型无关）。"""
    from engine.api import GameEngine
    from tests.attack_support import resolve_attack

    engine = GameEngine(rng_seed=7, db_path=str(tmp_path / "parry.db"),
                        save_dir=str(tmp_path / "saves"))
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=80,
                    current_hp=80, mana_limit=5, current_mana=5,
                    speed_limit=2, current_speed=2)
    monster = Entity(name="石像鬼", entity_type="怪物", blood_limit=80,
                     current_hp=80, mana_limit=3, current_mana=3,
                     speed_limit=3, current_speed=3)
    engine.state.player = player
    engine.state.enemies = [monster]
    engine.state.friends = []
    engine.state.employees = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 1

    speed_before = monster.current_speed
    out = resolve_attack(engine, parry=True)
    assert out.get("success"), out
    details = out["result"]["hits"]
    assert details[0].get("parry_success") is True
    assert monster.current_hp == 80, "怪物招架真刀应免疫该击伤害"
    assert monster.current_speed == speed_before, "招架真刀应返还速度"
