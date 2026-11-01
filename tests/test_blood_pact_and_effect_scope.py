"""【共罪】、再生6X与显式效果作用域账本的契约测试。"""
from __future__ import annotations

import pytest

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.enums import CombatSubphase, EffectPolarity, EffectScope
from engine.events import resolve_option_effect
from engine.models import (Consumable, DaoWen, DaoWenInstance, Entity, GameState,
                           Relic, Spell, StatusEffect)


def _state_with_blood_pact() -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase=CombatSubphase.PLAYER_ACTIONS.value)
    player = Entity(
        "轮回者", "轮回者", blood_limit=100, current_hp=100,
        mana_limit=30, current_mana=30, speed_limit=20, current_speed=20,
        attack_count=2, attack_power=4,
    )
    ally = Entity(
        "同伴", "朋友", blood_limit=80, current_hp=80,
        mana_limit=20, current_mana=20, speed_limit=14, current_speed=14,
        attack_count=2, attack_power=3,
    )
    state.player = player
    state.friends.append(ally)
    state.relics.append(Relic("共罪", "数值代价共同承担"))
    return state, CombatEngine(state, DiceEngine()), player, ally


def _bind_gongzui(caster: Entity, targets: list[Entity]) -> None:
    """建立与现行道纹一致的支付名额：两个目标之外，发动者始终追加一份。"""
    slots = list(targets) + [caster]
    group_ids = [str(entity.runtime_id) for entity in slots]
    unique = {str(entity.runtime_id): entity for entity in slots}
    for entity in unique.values():
        entity.add_status(StatusEffect(
            name="共罪", value=1, remaining_rounds=10, source="test",
            effect_data={"group_ids": group_ids, "source_daowen": "共罪"}))


def _bind_xueqi(payer: Entity, target: Entity, multiplier: int = 2) -> None:
    """血契只读取支付名义量，并让目标追加支付同种资源。"""
    payer.add_status(StatusEffect(
        name="血契", value=multiplier, remaining_rounds=3, source="test",
        effect_data={"target_id": str(target.runtime_id), "source_daowen": "血契"}))


def _game_with_three_slots(tmp_path):
    """三个不同实体各占一个名额，并接入真实 GameEngine 物品入口。"""
    state, combat, player, ally = _state_with_blood_pact()
    state.relics.clear()
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80,
                    mana_limit=20, current_mana=20)
    state.friends.append(second)
    _bind_gongzui(player, [ally, second])
    engine = GameEngine(
        db_path=str(tmp_path / "rules.db"), save_dir=str(tmp_path / "saves"),
        death_book_path=str(tmp_path / "death.md"),
        sealed_candidate_path=str(tmp_path / "sealed.json"), rng_seed=19)
    engine.state = state
    engine.combat = combat
    return engine, player, ally, second


# 2026-10-12 用户令：【共罪】删掉与道纹【透支】重合的「[回始]生命4X换X法力」，
# 只保留平分。原先此处有 3 条测试专测那个回始付费决策，驱动已被删除，
# 按「唯一驱动是已删机制的测试应删除而非改写」处理；平分功能的覆盖仍在
# test_blood_pact_shares_every_supported_numeric_cost /
# test_event_and_daowen_costs_use_blood_pact_bus 等处。

@pytest.mark.parametrize(
    ("cost_type", "player_field"),
    [
        ("生命", "current_hp"),
        ("血限", "blood_limit"),
        ("法力", "current_mana"),
        ("法限", "mana_limit"),
        ("速度", "current_speed"),
        ("速限", "speed_limit"),
    ],
)

def test_blood_pact_shares_every_supported_numeric_cost(
    cost_type: str, player_field: str,
):
    """六种资源共用总线；两个目标与发动者追加名额共同承担总支付5。"""
    state, combat, player, ally = _state_with_blood_pact()
    state.relics.clear()
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80,
                    mana_limit=20, current_mana=20, speed_limit=14, current_speed=14)
    state.friends.append(second)
    _bind_gongzui(player, [ally, second])
    members = [player, ally, second]
    before = [getattr(entity, player_field) for entity in members]

    result = combat.pay_resource(player, cost_type, 5)

    losses = [old - getattr(entity, player_field)
              for old, entity in zip(before, members)]
    assert sorted(losses) == [1, 2, 2]
    assert result["owner"]["paid"] + sum(
        part["paid"] for part in result["shared_with"]) == 5
    assert result["actual_paid"] == 5


def test_blood_pact_cost_one_and_dragon_heart_order():
    """代价1只落一个名额；龙心先抵3，再由三个名额平分剩余6。"""
    state, combat, player, ally = _state_with_blood_pact()
    state.relics.clear()
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80)
    state.friends.append(second)
    _bind_gongzui(player, [ally, second])
    one = combat.pay_resource(player, "生命", 1)
    assert sorted([100 - player.current_hp, 80 - ally.current_hp,
                   80 - second.current_hp]) == [0, 0, 1]
    assert one["owner"]["paid"] + sum(
        part["paid"] for part in one["shared_with"]) == 1

    heart = Consumable(
        "生命龙心", "抵消生命", current_uses=3, max_uses=3,
        kind="dragon_heart", dragon_heart_type="生命")
    state.consumables.append(heart)
    result = combat.pay_resource(player, "生命", 9, dragon_heart_use=3)
    assert result["dragon_heart_offset"] == 3
    assert result["remaining"] == 6
    # 首次随机1加本次每人2：三人累计承担分别为2/2/3。
    assert sorted([100 - player.current_hp, 80 - ally.current_hp,
                   80 - second.current_hp]) == [2, 2, 3]
    assert heart.current_uses == 0


def test_blood_pact_rejects_invalid_or_unpayable_share_atomically():
    """任一名额资源不足或提交旧手动分担引用时，整笔支付原子拒绝。"""
    state, combat, player, ally = _state_with_blood_pact()
    state.relics.clear()
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80,
                    mana_limit=20, current_mana=20)
    state.friends.append(second)
    _bind_gongzui(player, [ally, second])
    ally.current_mana = ally.mana_limit = 1
    before = [(entity.mana_limit, entity.current_mana)
              for entity in (player, ally, second)]
    with pytest.raises(ValueError, match="无法完整承担"):
        combat.pay_resource(player, "法限", 15)
    assert [(entity.mana_limit, entity.current_mana)
            for entity in (player, ally, second)] == before

    with pytest.raises(ValueError, match="不接受单目标分担引用"):
        combat.pay_resource(player, "生命", 5, cost_share_target_ref="friend:99")
    assert player.current_hp == 100 and ally.current_hp == 80








def test_event_and_daowen_costs_use_blood_pact_bus(tmp_path):
    """事件与道纹都走同一个自动共罪组，不需要旧遗物或手动分担参数。"""
    state, combat, player, ally = _state_with_blood_pact()
    state.relics.clear()
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80)
    state.friends.append(second)
    _bind_gongzui(player, [ally, second])
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state

    event = resolve_option_effect("生命15。", engine, params={})
    assert "error" not in event, event
    assert [100 - player.current_hp, 80 - ally.current_hp,
            80 - second.current_hp] == [5, 5, 5]

    target = Entity("敌人", "怪物", blood_limit=100, current_hp=100)
    state.enemies.append(target)
    calc = DaoWenEngine.resolve("连击", 5, target=target)
    combat.apply_daowen_effect("连击", calc, player, target)
    # 连击5支付生命25，三个名额分为8/8/9；连同事件15，总支付40。
    losses = sorted([100 - player.current_hp, 80 - ally.current_hp,
                     80 - second.current_hp])
    assert losses == [13, 13, 14]


def test_gongzui_daowen_binds_two_targets_who_share_resource_payments(tmp_path):
    """两个目标未选发动者时，发动者仍入组；之后三方都承担资源支付份额。"""
    state, combat, player, first = _state_with_blood_pact()
    state.relics.clear()
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80,
                    mana_limit=20, current_mana=20, speed_limit=14, current_speed=14)
    state.friends.append(second)
    player.dao_wen["共罪"] = DaoWenInstance(DaoWen(
        name="共罪", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    before_mana = player.current_mana
    before_hp = player.current_hp

    engine = GameEngine(db_path=str(tmp_path / "blood-pact-daowen.db"))
    engine.state = state
    engine.combat.state = state
    result = engine.execute_action("use_daowen", {
        "daowen_name": "共罪", "x": 2,
        "dodge_targets": [
            {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
            {"target_ref": "friend:1", "dodge": False, "blood_shadow": False},
        ],
    })
    assert result["success"] is True, result
    # 2026-10-31：共罪支付改为流血（生命）5X → X=2 扣 10 点生命，法力不动。
    assert player.current_hp == before_hp - 10
    assert player.current_mana == before_mana
    assert any(effect.get("targets") == [first.name, second.name, player.name]
               for effect in result["execution"]["effects"])
    members = [player, first, second]
    assert all(member.has_status("共罪") for member in members)
    assert all(len(member.get_status_effects("共罪")[0].effect_data["group_ids"]) == 3
               for member in members)
    assert combat._blood_pact_group(player) == [first, second, player]
    # 持续∞ 记 -1（与既有 ∞ 口径一致：仍只代表本场战斗内的无限）。
    assert first.get_status_effects("共罪")[0].remaining_rounds == -1

    # 基线取发动后的血量：共罪自身代价（生命5X）已经扣过，不能算进这次分摊。
    hp_after_cast, first_hp_after_cast, second_hp_after_cast = (
        player.current_hp, first.current_hp, second.current_hp)
    receipt = combat.pay_numeric_cost(player, "生命", 9)
    assert [hp_after_cast - player.current_hp,
            first_hp_after_cast - first.current_hp,
            second_hp_after_cast - second.current_hp] == [3, 3, 3]
    assert receipt["owner"]["paid"] == 3, "发动者自己的份额不得记成0或跳过"
    assert receipt["actual_paid"] == 9
    assert sum(part["paid"] for part in receipt["shared_with"]) == 6


def test_gongzui_targets_must_be_non_self_and_caster_still_adds_a_slot(tmp_path):
    """2026-10-31：共罪改选 X 个非自身目标；发动者仍额外占一个支付名额。"""
    state, combat, player, ally = _state_with_blood_pact()
    state.relics.clear()
    player.dao_wen["共罪"] = DaoWenInstance(DaoWen(
        name="共罪", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine = GameEngine(db_path=str(tmp_path / "blood-pact-caster-target.db"))
    engine.state = state
    engine.combat.state = state
    result = engine.execute_action("use_daowen", {
        "daowen_name": "共罪", "x": 2,
        "dodge_targets": [
            {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
            {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
        ],
    })
    # 自身不再可作为共罪目标（条文改「选择X个非自身目标」）。
    assert result["success"] is False and "非自身目标" in result["error"], result

    result = engine.execute_action("use_daowen", {
        "daowen_name": "共罪", "x": 2,
        "dodge_targets": [
            {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
            {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
        ],
    })
    assert result["success"] is False, result

    result = engine.execute_action("use_daowen", {
        "daowen_name": "共罪", "x": 2,
        "dodge_targets": [
            {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
        ],
    })
    assert result["success"] is False and "恰好2个" in result["error"], result

    result = engine.execute_action("use_daowen", {
        "daowen_name": "共罪", "x": 1,
        "dodge_targets": [
            {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
        ],
    })
    assert result["success"] is True, result
    # 一个非自身目标 + 发动者追加一份 = 两个支付名额。
    assert combat._blood_pact_group(player) == [ally, player]
    player_status = player.get_status_effects("共罪")[0]
    assert player_status.remaining_rounds == -1, "持续∞记 -1"
    assert player_status.effect_data["group_ids"].count(str(player.runtime_id)) == 1

    # 共罪自身代价 5 点生命已扣（X=1 → 5）；之后 5 点生命由两名额平分。
    hp_after_cast, ally_hp_after_cast = player.current_hp, ally.current_hp
    receipt = combat.pay_numeric_cost(player, "生命", 5)
    player_loss, ally_loss = hp_after_cast - player.current_hp, ally_hp_after_cast - ally.current_hp
    # 两个名额平分 5：余数按随机分配，方向不固定（(3,2)/(2,3) 都合法）。
    assert sorted((player_loss, ally_loss)) == [2, 3]
    assert receipt["owner"]["paid"] == player_loss
    assert receipt["shared_with"][0]["paid"] == ally_loss
    assert receipt["actual_paid"] == 5


def test_regeneration_is_four_x_and_old_contracts_are_removed():
    """再生为4X；共罪属于道纹，不在遗物池。"""
    target = Entity("目标", "朋友", blood_limit=100, current_hp=10)
    assert DaoWenEngine.resolve("再生", 4, target=target)["target_heal"] == 16
    names = {name for name, _ in GameEngine.RELIC_DEFS}
    assert "共罪" not in names
    assert "鲜血契约" not in names
    assert "卖身契" not in names
    assert "钱袋" not in names  # 钱袋已删除（其效果一度并入【第一杯】，2026-09-23【第一杯】重做时废止）
    # 遗物池数量会因其他资源迁移变化；此处只锁定本机制的资源类型。
    assert "共罪" in DaoWenEngine._registry


def test_scoped_ledger_rolls_back_battle_effects_but_keeps_costs(tmp_path):
    """正常路径：局内面板变化战终逆向清除；同字段上的血限代价仍然保留。"""
    state, combat, player, _ = _state_with_blood_pact()
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    state.current_battle = 1
    state.apply_scoped_delta(
        player, "blood_limit", 10,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
        source="增殖")
    state.apply_scoped_delta(
        player, "attack_power", 5,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
        source="全力")
    combat.pay_numeric_cost(player, "血限", 3)
    player.total_healed = 17
    player.no_action_rounds = 4
    player.no_damage_rounds = 3
    player._jisu_dodges = 1
    player.is_flying = True
    player.add_status(StatusEffect("全力", -1, 5, "全力"))

    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state
    result = engine.execute_action("battle_end", {})

    assert result["success"]
    assert player.blood_limit == 97  # 初始100，仅保留血限3
    assert player.attack_power == 4
    assert player.total_healed == 0
    assert player.no_action_rounds == player.no_damage_rounds == 0
    assert not hasattr(player, "_jisu_dodges")
    assert player.is_flying is False
    assert player.status_effects == []
    assert state.scoped_effect_ledger == []


def test_blood_limit_debuff_rolls_back_without_erasing_life_loss():
    """关键边界：伤痕造成的局内血限减少战终清除，当前生命损失不能被回滚顺带治疗。"""
    from engine.models import StatusEffect
    state = GameState(phase="in_combat")
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    current_mana=10)
    target = Entity("目标", "怪物", blood_limit=100, current_hp=100)
    state.player = player
    state.enemies = [target]
    combat = CombatEngine(state, DiceEngine())
    # 伤痕X=5：目标每次掉血后血限-5（切割道纹已删除，2026-08-21）
    target.add_status(StatusEffect(name="伤痕", remaining_rounds=3, value=5))
    combat._apply_hostile_damage(target, 5, source=player)
    assert target.blood_limit == 95 and target.current_hp == 95
    state.rollback_scoped_effects(EffectScope.BATTLE.value)

    assert target.blood_limit == 100
    assert target.current_hp == 95


def test_duration_expiry_rolls_back_matching_scoped_delta():
    """边界：持续效果到期时立即按来源回滚，不错误拖到战终。"""
    state = GameState(phase="in_combat")
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    attack_count=2, attack_power=10)
    state.player = player
    state.apply_scoped_delta(
        player, "attack_power", -7,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.DEBUFF.value,
        source="弱化")
    player.add_status(StatusEffect("弱化", 1, 1, "施法者"))
    assert player.attack_power == 3

    CombatEngine(state, DiceEngine()).round_end()

    assert player.attack_power == 10
    assert not player.has_status("弱化")
    assert state.scoped_effect_ledger == []


def test_scoped_ledger_survives_versioned_save_round_trip(tmp_path):
    """存档边界：作用域账本与稳定实体ID完整往返，载入后仍能回滚到当前实体。"""
    state, _, player, _ = _state_with_blood_pact()
    state.apply_scoped_delta(
        player, "attack_power", 6,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
        source="全力")
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state
    assert engine.save_game("scope")["version"] == 5
    player.attack_power = 99

    loaded = engine.load_game("scope")

    assert loaded["success"] and engine.state.player.attack_power == 10
    assert len(engine.state.scoped_effect_ledger) == 1
    engine.state.rollback_scoped_effects(EffectScope.BATTLE.value)
    assert engine.state.player.attack_power == 4


def test_status_scope_and_polarity_are_explicit():
    """状态元数据不再靠数值正负猜测：作用域与增减益极性分别可读。"""
    entity = Entity("角色", "朋友")
    entity.add_status(StatusEffect("全力", -1, 2, "道纹"))
    entity.add_status(StatusEffect("坏死", 2, 1, "道纹"))
    assert entity.get_status_effects("全力")[0].scope == EffectScope.BATTLE.value
    assert entity.get_status_effects("全力")[0].polarity == EffectPolarity.BUFF.value
    assert entity.get_status_effects("坏死")[0].polarity == EffectPolarity.DEBUFF.value

def test_legacy_gongzui_relic_has_no_payment_effect():
    """旧存档即使还留有共罪遗物，也不恢复回始主动或手动分担玩法。"""
    from engine.ai_rules import round_start_relic_choices
    from engine.models import Relic as _Relic

    e = GameEngine(db_path="/tmp/linji_tests/blood_pact_no_round_start.db", rng_seed=5)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8,
                      "mana_points": 6})
    from tests.setup_support import finish_initial_daowen
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.relics.append(_Relic("共罪", "数值型代价可与朋友/员工平分"))
    ally = Entity("同伴", "朋友", blood_limit=80, current_hp=80)
    e.state.friends.append(ally)
    e.state.energy = 0
    assert e.execute_action("battle_start", {"relic_choices": {}})["success"]

    # ① 不再是回始主动遗物
    choices = round_start_relic_choices(e)
    assert "共罪" not in choices, f"共罪已无回始主动效果：{choices}"
    r = e.execute_action("round_start", {"relic_choices": choices})
    assert r.get("success"), r

    # 旧遗物既不会自动分摊，也不能通过旧参数手动分摊。
    hp_p, hp_a = e.state.player.current_hp, ally.current_hp
    e.combat.pay_resource(e.state.player, "生命", 10)
    assert e.state.player.current_hp == hp_p - 10
    assert ally.current_hp == hp_a
    with pytest.raises(ValueError, match="共罪"):
        e.combat.pay_resource(e.state.player, "生命", 5,
                              cost_share_target_ref="friend:0")


def test_manual_cost_share_param_is_retired():
    """没有现行共罪组时，旧cost_share_target_ref一律不能授权分担。"""
    state, combat, player, _ally = _state_with_blood_pact()
    state.relics = [r for r in state.relics if r.name != "共罪"]
    with pytest.raises(ValueError, match="共罪"):
        combat.validate_numeric_cost(player, "生命", 5,
                                     cost_share_target_ref="friend:0")


def test_active_soil_mana_payment_uses_gongzui_bus(tmp_path):
    """活性土壤不再直接扣发动者法力；3点由三个支付名额各承担1点。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
    engine.state.current_round = 0
    engine.state.consumables = [Consumable("活性土壤", "", 1, 1)]
    before = [entity.current_mana for entity in (player, ally, second)]
    result = engine.execute_action("consume_item", {
        "name": "活性土壤", "x": 3, "dm_approved": True,
        "friend": {"name": "新同伴", "attack_count": 1,
                   "attack_power": 1, "blood_limit": 6},
    })
    assert result["success"], result
    after = [entity.current_mana for entity in (player, ally, second)]
    assert [old - new for old, new in zip(before, after)] == [1, 1, 1]
    payment = result["result"]["mana_payment"]
    assert payment["actual_paid"] == 3 and payment["owner"]["paid"] == 1
    assert engine.state.friends[-1].name == "新同伴"


def test_huangfu_imprint_mana_payment_uses_gongzui_bus(tmp_path):
    """黄符刻印支付也经过共罪，不在物品处理器里另扣一份。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    player.spells = [Spell(name="刻印术", required_daowen=[],
                           trigger_condition="瞬发", effect_flow="",
                           rank=3, trigger="瞬发")]
    engine.state.consumables = [Consumable("黄符", "", 1, 1)]
    before = sum(entity.current_mana for entity in (player, ally, second))
    result = engine.execute_action("consume_item", {
        "name": "黄符", "imprint_spell": "刻印术"})
    assert result["success"], result
    after = sum(entity.current_mana for entity in (player, ally, second))
    assert before - after == 3
    assert result["result"]["mana_payment"]["actual_paid"] == 3
    assert "【刻印：刻印术】" in engine.state.consumables[0].effect


def test_huangfu_spell_mana_payment_uses_gongzui_bus(tmp_path):
    """黄符能源与后续杀伐步骤都走总线，合计4点只结算一次。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    engine.state.current_round = 1
    ally.spells = [Spell(name="黄符杀伐", required_daowen=["杀伐"],
                         trigger_condition="瞬发", effect_flow="发动杀伐X于目标",
                         rank=3, trigger="瞬发")]
    engine.state.huangfu_grants().add((ally.runtime_id, "黄符杀伐"))
    engine.state.enemies = [Entity("靶怪", "怪物", blood_limit=100, current_hp=100)]
    before = sum(entity.current_mana for entity in (player, ally, second))
    result = engine.execute_action("cast", {
        "actor_ref": "friend:0", "huangfu_spell": "黄符杀伐", "flow": "占位",
        "target_ref": "enemy:0",
        "steps": [{"x": 1, "target_ref": "enemy:0", "dodge": False,
                   "trigger_spell_choices": {}}],
    })
    assert result["success"], result
    after = sum(entity.current_mana for entity in (player, ally, second))
    assert before - after == 4  # 黄符名义3 + 杀伐法力1
    payment = result["result"]["huangfu_mana_payment"]
    assert payment["actual_paid"] == 3 and payment["nominal_amount"] == 3
    assert engine.state.enemies[0].current_hp == 99


def test_event_shard_payment_uses_gongzui_bus(tmp_path):
    """事件的碎片支出同样由共罪名额承担，不再只改轮回者余额。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    engine.state.shards = 30
    ally.shards = second.shards = 20

    result = resolve_option_effect("失去6[碎片]。", engine, params={})

    assert "error" not in result, result
    assert engine.state.shards == 28
    assert ally.shards == second.shards == 18


def test_active_soil_payment_failure_is_atomic(tmp_path):
    """任一共罪名额法力不足时，不创生、不耗耐久，也不扣任何名额的法力。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
    engine.state.current_round = 0
    engine.state.consumables = [Consumable("活性土壤", "", 1, 1)]
    for entity in (player, ally, second):
        entity.current_mana = 0
    before_actions = player.actions_used_this_round
    before_friends = len(engine.state.friends)

    result = engine.execute_action("consume_item", {
        "name": "活性土壤", "x": 3, "dm_approved": True,
        "friend": {"name": "不应创生", "attack_count": 1,
                   "attack_power": 1, "blood_limit": 6},
    })

    assert result["success"] is False
    assert "支付失败" in result["error"]
    assert [entity.current_mana for entity in (player, ally, second)] == [0, 0, 0]
    assert len(engine.state.friends) == before_friends
    assert engine.state.consumables[0].current_uses == 1
    assert player.actions_used_this_round == before_actions


def test_huangfu_imprint_payment_failure_is_atomic(tmp_path):
    """刻印能源不足时不写入刻印、不耗黄符耐久，也不产生部分扣款。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    player.spells = [Spell(name="刻印术", required_daowen=[],
                           trigger_condition="瞬发", effect_flow="",
                           rank=3, trigger="瞬发")]
    engine.state.consumables = [Consumable("黄符", "", 1, 1)]
    for entity in (player, ally, second):
        entity.current_mana = 0
    before_actions = player.actions_used_this_round

    result = engine.execute_action("consume_item", {
        "name": "黄符", "imprint_spell": "刻印术"})

    assert result["success"] is False
    assert "支付失败" in result["error"]
    assert [entity.current_mana for entity in (player, ally, second)] == [0, 0, 0]
    assert engine.state.consumables[0].current_uses == 1
    assert "【刻印：" not in engine.state.consumables[0].effect
    assert player.actions_used_this_round == before_actions


def test_huangfu_spell_payment_failure_cleans_temp_daowen(tmp_path):
    """黄符能源支付失败时，不扣出手，且临时授予的杀伐必须收回。"""
    engine, player, ally, second = _game_with_three_slots(tmp_path)
    engine.state.current_round = 1
    ally.spells = [Spell(name="黄符杀伐", required_daowen=["杀伐"],
                         trigger_condition="瞬发", effect_flow="发动杀伐X于目标",
                         rank=3, trigger="瞬发")]
    engine.state.huangfu_grants().add((ally.runtime_id, "黄符杀伐"))
    engine.state.enemies = [Entity("靶怪", "怪物", blood_limit=100, current_hp=100)]
    for entity in (player, ally, second):
        entity.current_mana = 0
    before_actions = ally.actions_used_this_round

    result = engine.execute_action("cast", {
        "actor_ref": "friend:0", "huangfu_spell": "黄符杀伐",
        "target_ref": "enemy:0",
        "steps": [{"x": 1, "target_ref": "enemy:0", "dodge": False,
                   "trigger_spell_choices": {}}],
    })

    assert result["success"] is False
    assert [entity.current_mana for entity in (player, ally, second)] == [0, 0, 0]
    assert ally.actions_used_this_round == before_actions
    assert "杀伐" not in ally.dao_wen
    assert engine.state.enemies[0].current_hp == 100


def test_active_soil_payment_receipt_includes_xueqi(tmp_path):
    """活性土壤名义法力3触发血契2：目标额外支付法力6并写入同一回执。"""
    engine, player, _ally, _second = _game_with_three_slots(tmp_path)
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
    engine.state.current_round = 0
    engine.state.consumables = [Consumable("活性土壤", "", 1, 1)]
    enemy = Entity("血契目标", "怪物", blood_limit=100, current_hp=100,
                   mana_limit=20, current_mana=20)
    engine.state.enemies = [enemy]
    _bind_xueqi(player, enemy, 2)

    result = engine.execute_action("consume_item", {
        "name": "活性土壤", "x": 3, "dm_approved": True,
        "friend": {"name": "新同伴", "attack_count": 1,
                   "attack_power": 1, "blood_limit": 6},
    })

    assert result["success"], result
    payment = result["result"]["mana_payment"]
    assert payment["nominal_amount"] == 3
    assert enemy.current_mana == 14
    echo = payment["blood_pact_effects"][0]
    assert echo["nominal_amount"] == 3 and echo["multiplier"] == 2
    assert echo["payment"]["actual_paid"] == 6


def test_huangfu_imprint_payment_receipt_includes_xueqi(tmp_path):
    """黄符刻印的血契追加支付由总线生成，物品入口不另写一套。"""
    engine, player, _ally, _second = _game_with_three_slots(tmp_path)
    player.spells = [Spell(name="刻印术", required_daowen=[],
                           trigger_condition="瞬发", effect_flow="",
                           rank=3, trigger="瞬发")]
    engine.state.consumables = [Consumable("黄符", "", 1, 1)]
    enemy = Entity("血契目标", "怪物", blood_limit=100, current_hp=100,
                   mana_limit=20, current_mana=20)
    engine.state.enemies = [enemy]
    _bind_xueqi(player, enemy, 2)

    result = engine.execute_action("consume_item", {
        "name": "黄符", "imprint_spell": "刻印术"})

    assert result["success"], result
    payment = result["result"]["mana_payment"]
    assert payment["nominal_amount"] == 3
    assert enemy.current_mana == 14
    assert payment["blood_pact_effects"][0]["payment"]["actual_paid"] == 6


def test_huangfu_spell_payment_receipt_includes_xueqi(tmp_path):
    """黄符能源与后续效果步骤分别按自己的名义量触发血契。"""
    engine, _player, ally, _second = _game_with_three_slots(tmp_path)
    engine.state.current_round = 1
    ally.spells = [Spell(name="黄符杀伐", required_daowen=["杀伐"],
                         trigger_condition="瞬发", effect_flow="发动杀伐X于目标",
                         rank=3, trigger="瞬发")]
    engine.state.huangfu_grants().add((ally.runtime_id, "黄符杀伐"))
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100,
                   mana_limit=30, current_mana=30)
    engine.state.enemies = [enemy]
    _bind_xueqi(ally, enemy, 2)

    result = engine.execute_action("cast", {
        "actor_ref": "friend:0", "huangfu_spell": "黄符杀伐",
        "target_ref": "enemy:0",
        "steps": [{"x": 1, "target_ref": "enemy:0", "dodge": False,
                   "trigger_spell_choices": {}}],
    })

    assert result["success"], result
    payment = result["result"]["huangfu_mana_payment"]
    assert payment["nominal_amount"] == 3
    assert payment["blood_pact_effects"][0]["payment"]["actual_paid"] == 6
    # 能源名义3追加6；杀伐步骤名义1再追加2。
    assert enemy.current_mana == 22
    assert enemy.current_hp == 99
