"""已裁定遗物化道纹的运行时行为边界回归。

这些用例只钉住《物品索引》已有明文，不决定待裁定道纹的去留或新效果。
"""
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic, StatusEffect


def _combat():
    state = GameState()
    player = Entity(
        name="持有者", entity_type="轮回者",
        blood_limit=100, current_hp=50,
        mana_limit=20, current_mana=5,
        speed_limit=10, current_speed=3,
    )
    target = Entity(
        name="靶怪", entity_type="怪物",
        blood_limit=100, current_hp=100,
        mana_limit=10, current_mana=3,
        speed_limit=10, current_speed=2,
        attack_count=1, attack_power=5,
    )
    state.player = player
    state.enemies.append(target)
    state.phase = "in_combat"
    state.current_round = 2
    return state, player, target, CombatEngine(state, DiceEngine())


def test_baisu_gear_doubles_actual_speed_gain_and_respects_cap():
    state, player, _, combat = _combat()
    state.relics.append(Relic(name="倍速齿轮", effect="", tags=[]))

    assert combat._gain_speed(player, 2) == 4
    assert player.current_speed == 7

    assert combat._gain_speed(player, 1) == 2
    assert player.current_speed == 9
    assert combat._gain_speed(player, 1) == 1  # 最后1点受速限截断
    assert player.current_speed == player.speed_limit == 10


def test_nixue_longlin_adds_actual_life_loss_to_damage_for_this_round():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="逆血龙鳞", effect="", tags=[]))

    # 直接失血计入本回合实际失血账，随后造成的伤害增加相同数值。
    combat._raw_hp_loss(player, 3)
    result = combat._apply_hostile_damage(target, 5, source=player)

    assert result["actual_damage"] == 8
    assert target.current_hp == 92


def test_shishang_xuenang_heals_holder_from_actual_damage_taken_by_anyone():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="噬伤血囊", effect="", tags=[]))
    attacker = Entity(
        name="另一只怪物", entity_type="怪物",
        blood_limit=100, current_hp=100,
        mana_limit=10, current_mana=0,
        speed_limit=5, current_speed=1,
        attack_count=1, attack_power=1,
    )
    state.enemies.append(attacker)

    result = combat._apply_hostile_damage(target, 3, source=attacker)

    assert result["actual_damage"] == 3
    assert target.current_hp == 97
    assert player.current_hp == 51  # ceil(3 * 20%) = 1


def test_chongzhong_jiasuo_only_cuts_saved_action_of_enemy_who_targeted_holder():
    """跳过只扣本场锁定过持有者的敌方当前剩余出手，不再全场被动减手。"""
    state, player, target, combat = _combat()
    bystander = Entity("未选你的怪物", "怪物", blood_limit=20, current_hp=20,
                       mana_limit=1, current_mana=1, speed_limit=1, current_speed=1)
    state.enemies.append(bystander)
    state.relics.append(Relic(name="沉重枷锁", effect="", tags=[]))
    combat.round_start()
    base_player_actions = combat.single_round_action_count(player)
    base_target_actions = combat.single_round_action_count(target)

    # 只持有遗物不会削任何人；实际攻击即使零伤害也留下本场目标记录。
    assert combat.single_round_action_count(player) == base_player_actions == 1
    assert combat.single_round_action_count(target) == base_target_actions == 1
    combat.resolve_attack(target, player, allocated_damage=0)
    assert target.runtime_id in player.hostile_targeters_this_battle

    # 敌人有2手、已用1手并留1手后动；跳过后这份剩余出手立即归0。
    target.add_status(StatusEffect("疯狂", 1, 1, "test"))
    target.actions_used_this_round = 1
    assert combat.single_round_action_count(target) == 2
    effects = combat.resolve_heavy_shackles_on_skip(player)
    assert effects and effects[0]["target"] == target.name
    assert effects[0]["remaining_before"] == 1
    assert effects[0]["remaining_after"] == 0
    assert combat.single_round_action_count(target) == 1
    assert combat.single_round_action_count(bystander) == 1
    assert combat.single_round_action_count(player) == 1

    # 扣减只限当前回合；本场“曾选过你”的历史保留到后续回合。
    combat.round_start()
    assert combat.single_round_action_count(target) == 2
    assert target.runtime_id in player.hostile_targeters_this_battle


def test_chongzhong_jiasuo_does_nothing_when_target_has_no_remaining_action():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="沉重枷锁", effect="", tags=[]))
    combat.note_target_lock(target, player)
    target.actions_used_this_round = combat.single_round_action_count(target)

    assert combat.resolve_heavy_shackles_on_skip(player) == []
    assert target.heavy_shackles_action_penalty == 0
    assert combat.single_round_action_count(target) == 1


def test_shisu_mingdeng_reduces_speed_limit_only_when_speed_is_actually_lost():
    state, _, target, combat = _combat()
    state.relics.append(Relic(name="失速冥灯", effect="", tags=[]))

    assert combat._lose_current_speed(target, 1) == 1
    assert target.current_speed == 1
    assert target.speed_limit == 8

    assert combat._lose_current_speed(target, 99) == 1
    assert target.current_speed == 0
    assert target.speed_limit == 6

    assert combat._lose_current_speed(target, 1) == 0
    assert target.speed_limit == 6


def test_shijie_zhong_clock_erodes_each_living_roles_own_mana_times_speed():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="蚀界残钟", effect="", tags=[]))
    player.current_mana, player.current_speed = 5, 2
    target.blood_limit, target.current_hp = 80, 40
    target.current_mana, target.current_speed = 3, 2

    combat.round_end()

    assert player.blood_limit == 90
    assert target.blood_limit == 74
    assert player.current_hp == 50
    assert target.current_hp == 40


def test_shuai_bian_sha_lou_reduces_effect_x_by_one_but_clamps_at_zero():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="衰变沙漏", effect="", tags=[]))
    DaoWenEngine.register_all()

    calc = DaoWenEngine.resolve("杀伐", 2, target=target, caster=player)
    combat._apply_daowen_effect_impl("杀伐", calc, player, target)
    assert target.current_hp == 99  # X=2 的效果按 X=1 解析

    target.current_hp = target.blood_limit = 100
    calc = DaoWenEngine.resolve("杀伐", 1, target=target, caster=player)
    combat._apply_daowen_effect_impl("杀伐", calc, player, target)
    assert target.current_hp == 100  # X=1 降到 0，不得仍造成原伤害


def test_gongsheng_xuenang_heals_clone_from_actual_life_loss_and_transfers_overflow():
    state, creator, target, combat = _combat()
    state.relics.append(Relic(name="共生血囊", effect="", tags=[]))
    clone = Entity(
        name="持有者·裂1", entity_type="轮回者",
        blood_limit=30, current_hp=29,
        mana_limit=20, current_mana=5,
        speed_limit=10, current_speed=3,
        copy_creator_runtime_id=creator.runtime_id,
    )
    state.temp_friends.append(clone)

    result = combat._apply_hostile_damage(target, 5, source=clone)

    assert result["actual_damage"] == 5
    assert clone.current_hp == 30
    assert creator.current_hp == 54  # 克隆体只能补1，溢出的4转给创造者


def test_gongsheng_xuenang_uses_actual_life_loss_for_multi_target_damage():
    state, creator, target, combat = _combat()
    state.relics.append(Relic(name="共生血囊", effect="", tags=[]))
    clone = Entity(
        name="持有者·裂2", entity_type="轮回者",
        blood_limit=30, current_hp=10,
        mana_limit=20, current_mana=5,
        speed_limit=10, current_speed=3,
        copy_creator_runtime_id=creator.runtime_id,
    )
    target2 = Entity(
        name="第二靶怪", entity_type="怪物",
        blood_limit=100, current_hp=4,
        mana_limit=10, current_mana=3,
        speed_limit=10, current_speed=2,
    )
    state.temp_friends.append(clone)
    state.enemies.append(target2)

    first = combat._apply_hostile_damage(target, 3, source=clone)
    second = combat._apply_hostile_damage(target2, 9, source=clone)

    assert first["actual_damage"] == 3
    assert second.get("actual_life_loss") == 4  # 超杀部分不计入造物回复
    assert clone.current_hp == 17
    assert creator.current_hp == 50


def test_gongsheng_xuenang_is_inactive_after_creator_death():
    state, creator, target, combat = _combat()
    state.relics.append(Relic(name="共生血囊", effect="", tags=[]))
    clone = Entity(
        name="持有者·裂3", entity_type="轮回者",
        blood_limit=30, current_hp=10,
        mana_limit=20, current_mana=5,
        speed_limit=10, current_speed=3,
        copy_creator_runtime_id=creator.runtime_id,
    )
    state.temp_friends.append(clone)
    creator.current_hp = 0

    result = combat._apply_hostile_damage(target, 5, source=clone)

    assert result["actual_damage"] == 5
    assert clone.current_hp == 10
    assert creator.current_hp == 0
