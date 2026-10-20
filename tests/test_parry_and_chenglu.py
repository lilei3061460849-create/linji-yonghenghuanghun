"""回归：通用伤害/数值代价与逐击攻击防御分离，以及承露盏和属性上限。

逐击招架的真刀、空刀与速度返还由 tests/test_blade_combat_rework.py 覆盖。
"""
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _arena(mana=10, mana_limit=10, bl=100, hp=100, shield=0, relics=()):
    state = GameState()
    state.rng_seed = 1
    dice = DiceEngine(seed=1)
    player = Entity(name="P", entity_type="轮回者",
                    blood_limit=bl, current_hp=hp, mana_limit=mana_limit, current_mana=mana,
                    speed_limit=5, current_speed=5, attack_count=1, attack_power=0,
                    shield=shield)
    enemy = Entity(name="E", entity_type="怪物",
                   blood_limit=50, current_hp=50, mana_limit=0, current_mana=0,
                   speed_limit=4, current_speed=4, attack_count=1, attack_power=10)
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic(name=n, effect="") for n in relics]
    combat = CombatEngine(state, dice)
    return state, combat, player, enemy


# ==================== 1. 通用伤害与数值代价 ====================

def test_direct_damage_path_does_not_apply_attack_parry():
    """普通受伤入口不处理攻击招架，仍按引擎伤害结算。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    combat._apply_hostile_damage(player, 25, source=enemy)
    assert player.current_hp == 75


def test_numeric_cost_bypasses_the_shield_damage_channel():
    """边界：数值型代价全额扣生命，不进入普通伤害/格挡通道。"""
    _, combat, player, _ = _arena(bl=100, hp=100, shield=12)
    combat.pay_numeric_cost(player, "流血", 8, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 8, "tags": {"active_payment"}})
    assert player.current_hp == 92, "代价须全额支付"
    assert player.shield == 12, "格挡不吸收数值型代价"


# ==================== 2. 承露盏 ====================

def test_chenglu_accumulates_and_grants_mana():
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("承露盏",))
    combat._apply_hostile_damage(player, 5, source=enemy)
    assert player.current_mana == 0
    combat._apply_hostile_damage(player, 5, source=enemy)
    assert player.current_mana == 1, "累计失血10 → +1法力"


def test_chenglu_no_relic_no_mana():
    _, combat, player, enemy = _arena(mana=0, mana_limit=10)
    combat._apply_hostile_damage(player, 30, source=enemy)
    assert player.current_mana == 0


def test_chenglu_resets_between_battles():
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("承露盏",))
    combat._apply_hostile_damage(player, 9, source=enemy)
    assert player.hp_lost_this_battle == 9 and player.current_mana == 0
    combat.reset_monster_activation()
    assert player.hp_lost_this_battle == 0 and player.chenglu_paid == 0
    combat._apply_hostile_damage(player, 9, source=enemy)
    assert player.current_mana == 0


# ==================== 3. 全局上限 ====================

def test_all_attributes_capped_at_their_limits():
    _, combat, player, _ = _arena(mana=10, mana_limit=10)
    player.current_mana = 999
    player.current_speed = 999
    player.current_hp = 999
    combat.clamp_immortal_body(player)
    assert player.current_mana == player.mana_limit
    assert player.current_speed == player.speed_limit
    assert player.current_hp == player.blood_limit


def test_cap_does_not_block_refilling_a_spent_pool():
    _, combat, player, _ = _arena(mana=10, mana_limit=10)
    player.spend_mana(6)
    player.current_mana += 3
    combat.clamp_immortal_body(player)
    assert player.current_mana == 7


# ==================== 4. 流血代价 × 承露盏 ====================

def test_chenglu_feeds_the_touzhi_regeneration_loop():
    _, combat, player, _ = _arena(mana=0, mana_limit=20, bl=200, hp=200,
                                  relics=("承露盏",))
    combat.pay_numeric_cost(player, "流血", 12, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 12, "tags": {"active_payment"}})
    assert player.current_mana == 1, "透支流血12 → 承露盏返1法力"
    player.current_mana += 3
    combat.clamp_immortal_body(player)
    assert player.current_mana == 4
    assert player.current_hp == 188
