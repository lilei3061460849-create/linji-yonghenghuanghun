"""守夜灯、万钧印与物品文案的回归测试。"""
from pathlib import Path

from engine.api import GameEngine, TWISTED_TOOL_LIBRARY
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.mechanisms import Phase
from engine.models import Consumable, Entity, GameState, Relic

ROOT = Path(__file__).resolve().parent.parent


def test_shouyedeng_grants_mana_at_each_round_increase():
    """守夜灯按经历回合在[回增]获得当前世界回合数的法力。"""
    state = GameState()
    player = Entity(
        name="测试者", entity_type="轮回者",
        blood_limit=100, current_hp=100, mana_limit=100, current_mana=0,
    )
    state.player = player
    state.relics = [Relic(name="守夜灯", effect="")]
    combat = CombatEngine(state, DiceEngine())

    state.current_round = 1
    combat._dispatch_phase(Phase.ROUND_INCREASE, target=player)
    assert player.current_mana == 1

    state.current_round = 7
    combat._dispatch_phase(Phase.ROUND_INCREASE, target=player)
    assert player.current_mana == 8


def test_wanjunyin_is_a_three_durability_consumable_not_a_relic():
    assert all(name != "万钧印" for name, _ in GameEngine.RELIC_DEFS)
    assert TWISTED_TOOL_LIBRARY["万钧印"][0] == 3

    state = GameState()
    player = Entity(
        name="测试者", entity_type="轮回者",
        blood_limit=100, current_hp=100, mana_limit=10, current_mana=10,
    )
    attacker = Entity(
        name="攻击者", entity_type="怪物",
        blood_limit=100, current_hp=100, attack_count=1, attack_power=10,
    )
    state.player = player
    state.enemies.append(attacker)
    item = Consumable(
        name="万钧印", effect=TWISTED_TOOL_LIBRARY["万钧印"][1],
        current_uses=3, max_uses=3,
    )
    state.consumables.append(item)
    combat = CombatEngine(state, DiceEngine())

    first = combat.resolve_attack(attacker, player, allocated_damage=10)
    assert first["wanjunyin"]["converted_to"] == attacker.name
    assert item.current_uses == 2

    second = combat.resolve_attack(attacker, player, allocated_damage=10)
    assert second["wanjunyin"]["converted_to"] == attacker.name
    assert item.current_uses == 1

    third = combat.resolve_attack(attacker, player, allocated_damage=10)
    assert third["wanjunyin"]["converted_to"] == attacker.name
    assert item.current_uses == 0

    fourth = combat.resolve_attack(attacker, player, allocated_damage=10)
    assert "wanjunyin" not in fourth
    assert item.current_uses == 0


def test_relic_wording_uses_requested_concise_descriptions():
    index = (ROOT / "物品索引.md").read_text(encoding="utf-8")
    api = (ROOT / "engine/api.py").read_text(encoding="utf-8")
    assert "[回增]，你获得等同于增加后当前回合数的法力。" in index
    assert "任意角色每次失去生命时，使其支付血限1。" in index
    assert "本回合你每失去1点生命，造成的伤害+1。" in index
    assert "战斗被动" not in index
    # 缓厄沙漏的奇数延期按现行裁定向上取整，不再把必要结算术语当旧文案禁词。
    assert "延期的一半向上取整" in index
    assert "持有者" not in index
    assert "战斗被动" not in api
    assert '("噬伤血囊", "任意角色受到伤害时，你回复该次实际伤害的20%")' in api
    assert '("倍速齿轮", "你获得速度时，实际获得的速度翻倍")' in api
