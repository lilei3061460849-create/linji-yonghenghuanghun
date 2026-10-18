"""祭坛经济与动态还债阈值回归测试。"""
import tempfile

from engine.api import GameEngine
from engine.models import Entity
from tests.setup_support import finish_initial_daowen


def _engine(seed=31):
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "甲", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _stage(e, name, option):
    e.event_pool.events[name] = {
        "name": name, "region": "扭曲都市", "desc": "测试事件",
        "options": [{"id": 1, "text": option}],
    }
    e.event_pool.current = name


def test_shard_peak_only_increases():
    e = _engine()
    assert e.state.shards_peak == e.state.shards
    e.state.shards = 120
    assert e.state.shards_peak == 120
    e.state.shards = 5
    assert e.state.shards_peak == 120
    e.state.shards_peak = 2
    assert e.state.shards_peak == 120
    ally = Entity("测试朋友", "朋友", shards=30)
    assert ally.shards_peak == 30
    ally.shards = -5
    assert ally.shards_peak == 30
    ally.shards_peak = 1
    assert ally.shards_peak == 30


def test_altar_black_swan_records_debt_and_triggers_dynamic_death():
    e = _engine()
    e.state.shards = 20
    assert e.state.shards_peak == 20
    _stage(e, "祭坛", "投资：投入X[碎片]")
    e.dice.auto_roll = lambda name, options, context="": {
        "selected": 75, "record": {"seed": 31, "selected": 75, "context": context}}
    result = e.execute_action("resolve_event", {
        "event": "祭坛", "option_id": 1, "x": 20})
    assert result.get("success"), result
    assert e.state.shards == -40
    assert e.state.shards_peak == 20
    assert not e.state.player.is_alive
    assert e.state.player.departure_reason == "还债"
    assert result["result"]["outcome"] == "黑天鹅"
    assert "祭坛" not in e.event_pool.triggered, "祭坛可重复抵达，每次访问各投资一次"


def test_merchant_can_buy_and_sell_relics_at_confirmed_price():
    sell_engine = _engine(seed=32)
    relic_name = sell_engine.state.relics[0].name
    _stage(sell_engine, "过路商人", "遗物交易：自由买入或卖出遗物")
    sold = sell_engine.execute_action("resolve_event", {
        "event": "过路商人", "option_id": 1, "trade_direction": "sell",
        "relic_name": relic_name, "price": 15, "confirmed": True})
    assert sold.get("success"), sold
    assert not any(r.name == relic_name for r in sell_engine.state.relics)
    assert sell_engine.state.shards == 35

    buy_engine = _engine(seed=33)
    buy_engine.state.player.blood_limit = 60
    buy_engine._init_relic_pool()
    relic_to_buy = buy_engine.state.relics_pool[0].name
    _stage(buy_engine, "过路商人", "遗物交易：自由买入或卖出遗物")
    bought = buy_engine.execute_action("resolve_event", {
        "event": "过路商人", "option_id": 1, "trade_direction": "buy",
        "relic_name": relic_to_buy, "price": 10, "confirmed": True})
    assert bought.get("success"), bought
    assert any(r.name == relic_to_buy for r in buy_engine.state.relics)
    assert buy_engine.state.shards == 10


def test_non_monster_debt_threshold_causes_death_not_kill_reward():
    """朋友触及动态还债线时被吞噬离场，不按普通击杀结算。"""
    e = _engine(seed=34)
    ally = Entity("欠债朋友", "朋友", blood_limit=30, current_hp=30,
                  attack_count=1, attack_power=1, shards=-10)
    e.state.friends.append(ally)
    results = e.combat.settle_victory_paths()
    assert not ally.is_alive
    assert ally.current_hp == 0
    assert ally.departure_reason == "还债"
    assert any(r.get("type") == "debt_death" and r.get("entity") == "欠债朋友"
               for r in results)
