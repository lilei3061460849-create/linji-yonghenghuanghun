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
                           Relic, StatusEffect)


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


# 2026-10-12 用户令：【共罪】删掉与道纹【透支】重合的「[回始]流血4X换X法力」，
# 只保留平分。原先此处有 3 条测试专测那个回始付费决策，驱动已被删除，
# 按「唯一驱动是已删机制的测试应删除而非改写」处理；平分功能的覆盖仍在
# test_blood_pact_shares_every_supported_numeric_cost /
# test_event_and_daowen_costs_use_blood_pact_bus 等处。

@pytest.mark.parametrize(
    ("cost_type", "player_field", "ally_field"),
    [
        ("流血", "current_hp", "current_hp"),
        ("衰老", "blood_limit", "blood_limit"),
        ("枯竭", "mana_limit", "mana_limit"),
        ("萎缩", "speed_limit", "speed_limit"),
        ("疲惫", "current_speed", "current_speed"),
        ("异变", "mutation_count", "mutation_count"),
    ],
)

def test_blood_pact_shares_every_supported_numeric_cost(
    cost_type: str, player_field: str, ally_field: str,
):
    """正常路径：代价5与同伴平分（2/3，余数随机分配），六种数值代价使用同一总线。"""
    _, combat, player, ally = _state_with_blood_pact()
    player_before = getattr(player, player_field)
    ally_before = getattr(ally, ally_field)

    result = combat.pay_numeric_cost(
        player, cost_type, 5, cost_share_target_ref="friend:0")

    if cost_type == "异变":
        gains = [getattr(player, player_field) - player_before,
                 getattr(ally, ally_field) - ally_before]
    else:
        gains = [player_before - getattr(player, player_field),
                 ally_before - getattr(ally, ally_field)]
    assert sorted(gains) == [2, 3], "平分规则：总代价5平分，余数1随机分配"
    assert result["owner"]["paid"] + result["shared_with"]["paid"] == 5
    assert result["actual_paid"] == 5


def test_blood_pact_cost_one_and_dragon_heart_order():
    """边界：代价1平分（一方1/一方0，余数随机）；龙心先抵消，再拆分剩余后果。"""
    state, combat, player, ally = _state_with_blood_pact()
    one = combat.pay_numeric_cost(
        player, "流血", 1, cost_share_target_ref="friend:0")
    assert sorted([100 - player.current_hp, 80 - ally.current_hp]) == [0, 1]
    assert one["owner"]["paid"] + one["shared_with"]["paid"] == 1

    heart = Consumable(
        "流血龙心", "抵消流血", current_uses=3, max_uses=3,
        kind="dragon_heart", dragon_heart_type="流血")
    state.consumables.append(heart)
    result = combat.pay_numeric_cost(
        player, "流血", 9, cost_share_target_ref="friend:0", dragon_heart_use=3)
    assert result["dragon_heart_offset"] == 3
    assert result["remaining"] == 6
    # 总流血7（1随机分+6平分3/3）：双方合计7，各3或4。
    assert sorted([100 - player.current_hp, 80 - ally.current_hp]) == [3, 4]
    assert heart.current_uses == 0


def test_blood_pact_rejects_invalid_or_unpayable_share_atomically():
    """非法输入：无遗物、非法引用或共同承担者资源不足时，任何一方都不扣除。"""
    state, combat, player, ally = _state_with_blood_pact()
    ally.current_mana = ally.mana_limit = 1
    before = (player.mana_limit, ally.mana_limit)
    with pytest.raises(ValueError, match="无法完整承担"):
        combat.pay_numeric_cost(
            player, "枯竭", 5, cost_share_target_ref="friend:0")
    assert (player.mana_limit, ally.mana_limit) == before

    with pytest.raises(ValueError, match="cost_share_target_ref"):
        combat.pay_numeric_cost(
            player, "流血", 5, cost_share_target_ref="friend:99")
    assert player.current_hp == 100

    state.relics.clear()
    with pytest.raises(ValueError, match="共罪"):
        combat.pay_numeric_cost(
            player, "流血", 5, cost_share_target_ref="friend:0")
    assert player.current_hp == 100 and ally.current_hp == 80








def test_event_and_daowen_costs_use_blood_pact_bus(tmp_path):
    """正常路径：事件与道纹代价均接入同一共罪总线，不只覆盖遗物自身。"""
    state, combat, player, ally = _state_with_blood_pact()
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state

    event = resolve_option_effect(
        "流血15。", engine, params={"cost_share_target_ref": "friend:0"})
    assert "error" not in event
    # 平分规则（2026-08-21）：15平分 → 7/8（余数随机）
    assert sorted([100 - player.current_hp, 80 - ally.current_hp]) == [7, 8]

    target = Entity("敌人", "怪物", blood_limit=100, current_hp=100)
    state.enemies.append(target)
    calc = DaoWenEngine.resolve("连击", 5, target=target)
    combat.apply_daowen_effect(
        "连击", calc, player, target,
        cost_share_target_ref="friend:0")
    # 连击5流血25平分 → 12/13（余数随机）；两次合计 = 15+25 = 40，双方各承担19~21。
    losses = sorted([100 - player.current_hp, 80 - ally.current_hp])
    assert sum(losses) == 40
    assert losses[0] in (19, 20) and losses[1] in (20, 21)


def test_blood_pact_daowen_binds_x_targets_who_split_costs(tmp_path):
    """共罪X：支付10X法力；X个目标组成无限期分担组，代价只在目标间平分。"""
    state, combat, player, first = _state_with_blood_pact()
    state.relics.clear()  # 新能力来自道纹绑定，不靠遗物资格
    second = Entity("第二同伴", "朋友", blood_limit=80, current_hp=80,
                    mana_limit=20, current_mana=20, speed_limit=14, current_speed=14)
    state.friends.append(second)
    player.dao_wen["共罪"] = DaoWenInstance(DaoWen(
        name="共罪", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    before_mana = player.current_mana

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
    assert player.current_mana == before_mana - 20
    assert result["execution"]["effects"][0]["targets"] == [first.name, second.name]
    assert combat._blood_pact_group(player) == [first, second]

    receipt = combat.pay_numeric_cost(player, "流血", 9)
    assert player.current_hp == 100, "代价由共罪目标组承担，施法者不计入平分"
    assert sorted([80 - first.current_hp, 80 - second.current_hp]) == [4, 5]
    assert receipt["owner"]["paid"] == 0
    assert receipt["actual_paid"] == 9


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
    """正常路径：局内面板变化战终逆向清除；同字段上的衰老代价仍然保留。"""
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
    combat.pay_numeric_cost(player, "衰老", 3)
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
    assert player.blood_limit == 97  # 初始100，仅保留衰老3
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

def test_blood_pact_is_no_longer_a_round_start_active_relic():
    """2026-10-12 用户令：共罪的回始付费半已删，它不再是回始主动遗物。

    与【透支】（流血4X 换 X 法力）重合的那半功能移除后：
      · 回始决策里不得再出现 `共罪` 键（提交它也不该被接受）；
      · 平分作为其主要功能必须照常可用——走代价提交处的 cost_share_target_ref。
    """
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

    # ② 平分仍是其主要功能：带 cost_share_target_ref 支付数值代价，由朋友分担一半
    hp_p, hp_a = e.state.player.current_hp, ally.current_hp
    e.combat.pay_numeric_cost(e.state.player, "流血", 10,
                              cost_share_target_ref="friend:0")
    assert e.state.player.current_hp == hp_p - 5, "持有者应承担一半"
    assert ally.current_hp == hp_a - 5, "朋友应承担另一半"


def test_blood_pact_share_still_requires_holding_the_relic():
    """平分仍由【共罪】授权：没有它就不得提交 cost_share_target_ref。"""
    state, combat, player, _ally = _state_with_blood_pact()
    state.relics = [r for r in state.relics if r.name != "共罪"]
    with pytest.raises(ValueError, match="共罪"):
        combat.validate_numeric_cost(player, "流血", 5,
                                     cost_share_target_ref="friend:0")
