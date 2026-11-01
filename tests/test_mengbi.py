"""蒙蔽道纹退役后的入口与强光探照灯伤害无效契约。"""
from engine.daowen import DaoWenEngine
from engine.models import Consumable, DaoWen, DaoWenInstance
from tests.test_dingming_pill import ready


def _ready_with_lamp():
    engine, player, monster, _ = ready()
    item = Consumable("强光探照灯", "", 2, 2)
    engine.state.consumables = [item]
    return engine, player, monster, item


def test_retired_daowen_entry_is_rejected_without_cost():
    """即使旧存档手工残留道纹实例，也不能再从 use_daowen 发动。"""
    engine, player, monster, _ = _ready_with_lamp()
    player.dao_wen["蒙蔽"] = DaoWenInstance(
        DaoWen(name="蒙蔽", formula="", cost_type="消耗", cost_formula="2X", effect_formula=""))
    mana = player.current_mana
    actions = player.actions_used_this_round

    result = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 1, "target": monster.name,
    })

    assert not result["success"]
    assert player.current_mana == mana
    assert player.actions_used_this_round == actions
    assert not monster.has_status("蒙蔽")


def test_lamp_blocks_two_damage_instances_then_damage_returns():
    """物品固定施加两层；伤害类道纹（连击）连吃两次无效，第三次恢复。"""
    engine, player, monster, item = _ready_with_lamp()
    result = engine.execute_action("consume_item", {
        "name": item.name, "target_ref": "enemy:0",
    })
    assert result["success"], result
    hp = player.current_hp
    monster.current_mana = monster.mana_limit = 60

    def _lianji():
        calc = DaoWenEngine.resolve("连击", 1, target=player, caster=monster,
                                    cost_channel="法力")
        return engine.combat.apply_daowen_effect("连击", calc, monster, player)

    first = _lianji()
    assert first.get("mengbi_blocked") is True
    assert player.current_hp == hp
    assert monster.get_status_value("蒙蔽") == 1

    second = _lianji()
    assert second.get("mengbi_blocked") is True
    assert player.current_hp == hp
    assert not monster.has_status("蒙蔽")

    third = _lianji()
    assert not third.get("mengbi_blocked")
    assert player.current_hp < hp


def test_lamp_does_not_block_life_loss_daowen():
    """杀伐是「使[目标]生命-X²」——失去生命不是【伤害】，强光探照灯挡不住它。"""
    engine, player, monster, item = _ready_with_lamp()
    assert engine.execute_action("consume_item", {
        "name": item.name, "target_ref": "enemy:0",
    })["success"]
    hp = player.current_hp
    calc = DaoWenEngine.resolve("杀伐", 3, target=player, caster=monster)
    result = engine.combat.apply_daowen_effect("杀伐", calc, monster, player)
    assert not result.get("mengbi_blocked")
    assert player.current_hp == hp - 9
    assert monster.get_status_value("蒙蔽") == 2, "挡不住也不该消耗层数"


def test_lamp_spends_durability_not_mana_or_actions_and_rejects_exhaustion():
    engine, player, monster, item = _ready_with_lamp()
    mana = player.current_mana
    actions = player.actions_used_this_round

    for expected_uses, expected_stacks in ((1, 2), (0, 4)):
        result = engine.execute_action("consume_item", {
            "name": item.name, "target_ref": "enemy:0",
        })
        assert result["success"], result
        assert item.current_uses == expected_uses
        assert monster.get_status_value("蒙蔽") == expected_stacks
        assert player.current_mana == mana
        assert player.actions_used_this_round == actions

    result = engine.execute_action("consume_item", {
        "name": item.name, "target_ref": "enemy:0",
    })
    assert not result["success"]
    assert item.current_uses == 0
    assert monster.get_status_value("蒙蔽") == 4


def test_lamp_invalid_target_does_not_spend_durability():
    engine, player, monster, item = _ready_with_lamp()
    for target_ref in ("player:0", "enemy:99", ""):
        result = engine.execute_action("consume_item", {
            "name": item.name, "target_ref": target_ref,
        })
        assert not result["success"], result
        assert item.current_uses == 2
        assert not monster.has_status("蒙蔽")
