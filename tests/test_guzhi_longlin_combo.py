"""龙鳞遗物固定先减1，再结算固执；道纹序列不能重排固定遗物减伤。"""
from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect, Relic


def _arena(hp: int = 10_000, sequence: tuple[str, str] = ("龙鳞", "固执")) -> tuple[Entity, Entity, CombatEngine]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    target = Entity("受测者", "轮回者", blood_limit=hp, current_hp=hp,
                    mana_limit=10, current_mana=10, speed_limit=1, current_speed=1)
    source = Entity("攻击者", "怪物", blood_limit=hp, current_hp=hp,
                    attack_count=1, attack_power=1)
    order = {name: index for index, name in enumerate(sequence)}
    # 故意按「固执先施加」构造状态，并给 X 相反值，验证实际结算只认道纹序列位置。
    target.add_status(StatusEffect("固执", value=1, remaining_rounds=1, source="test",
                                   activation_x=999, daowen_order=order["固执"]))
    target.add_status(StatusEffect("龙鳞", value=1, remaining_rounds=-1, source="test",
                                   activation_x=1, daowen_order=order["龙鳞"]))
    state.relics.append(Relic("叠鳞甲", ""))
    state.player = target
    state.enemies = [source]
    return target, source, CombatEngine(state, DiceEngine(seed=20261009))


def test_guzhi_and_longlin_follow_daowen_sequence_not_x_or_application_order():
    """序列为龙鳞→固执时，龙鳞先减、固执后封顶：10^12 伤害最终是 1。"""
    target, source, combat = _arena()
    detail = combat._apply_hostile_damage(target, 10**12, source=source)

    # raw_damage 记录进入伤害管线前的原始值；实际扣血则验证龙鳞→固执的调整结果。
    assert detail["raw_damage"] == 10**12
    assert detail["actual_damage"] == 1
    assert target.current_hp == 9_999


def test_reversing_legacy_sequence_cannot_reverse_relic_adjustment_order():
    """龙鳞已不是道纹；固定减伤不能排在固执之后制造无限免伤。"""
    target, source, combat = _arena(sequence=("固执", "龙鳞"))
    detail = combat._apply_hostile_damage(target, 5, source=source)
    assert detail["actual_damage"] == 1
    assert target.current_hp == 9_999


def test_daowen_sequence_position_survives_entity_seal_roundtrip():
    """封存/还原必须保留道纹位置，不能让旧 X 或施加时间接管顺序。"""
    target, _, _ = _arena()
    for name in ("龙鳞", "固执"):
        target.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
            x_value=1,
        )
    engine = GameEngine.__new__(GameEngine)
    engine.state = GameState(player=target)
    sealed = engine._serialize_entity_full(target)
    restored = engine._deserialize_entity_full(sealed)
    assert list(restored.dao_wen) == ["龙鳞", "固执"]
    assert {s.name: s.daowen_order for s in restored.status_effects} == {"固执": 1, "龙鳞": 0}
    assert restored.ordered_statuses()[0].name == "龙鳞"

    # 旧封存没有 daowen_order：同名道纹状态从已保存的道纹序列恢复位置。
    legacy = engine._serialize_entity_full(target)
    for status in legacy["status_effects"]:
        status.pop("daowen_order", None)
    legacy_restored = engine._deserialize_entity_full(legacy)
    assert legacy_restored.ordered_statuses()[0].name == "龙鳞"


def test_legacy_pickle_statuses_get_sequence_position_during_load_migration():
    """旧 pickle 实例缺少新字段时，读档迁移不报错并恢复同名道纹顺序。"""
    target, _, _ = _arena()
    for name in ("龙鳞", "固执"):
        target.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
            x_value=1,
        )
    for status in target.status_effects:
        if "daowen_order" in vars(status):
            delattr(status, "daowen_order")
    engine = GameEngine.__new__(GameEngine)
    engine.state = GameState(player=target)
    engine._migrate_legacy_daowen_status_order()
    assert target.ordered_statuses()[0].name == "龙鳞"


def test_guzhi_longlin_zeroes_only_hits_within_longlin_threshold_and_each_large_hit_still_costs_one():
    """≤龙鳞值的单击为0；超过后每一笔独立伤害仍掉1，故不能免任意多击。"""
    target, source, combat = _arena()

    small = combat._apply_hostile_damage(target, 1, source=source)
    assert small["actual_damage"] == 0
    assert target.current_hp == 10_000

    for _ in range(7):
        detail = combat._apply_hostile_damage(target, 999_999, source=source)
        assert detail["actual_damage"] == 1
    assert target.current_hp == 9_993, "七笔独立大伤害应各穿过 1 点"


def test_guzhi_longlin_apply_to_piercing_and_cost_still_bypasses_longlin():
    """无视格挡不绕过组合；代价伤害绕过龙鳞，但**不再**绕过固执。

    2026-10-11 用户令：固执简化为「你每次失去的生命不会超过1，持续X」，
    代价路径与直接失血一并封顶。旧口径「代价同时绕过龙鳞与固执」中，
    绕过龙鳞的部分仍然成立（代价不经伤害调整阶段），绕过固执的部分已废止。
    """
    target, source, combat = _arena()

    piercing = combat._apply_hostile_damage(target, 500, damage_type="无视格挡", source=source)
    assert piercing["actual_damage"] == 1

    # 用新的满血靶，避免上一击干扰。代价不经龙鳞，但仍被固执封顶为1。
    cost_target, cost_source, cost_combat = _arena(hp=2_000)
    cost = cost_combat._apply_hostile_damage(cost_target, 999, damage_type="代价", source=cost_source)
    assert cost["raw_damage"] == 999
    assert cost["actual_damage"] == 1
    assert cost_target.current_hp == 1_999
