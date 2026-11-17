"""探索一/二档、事件队列与待结算门禁。"""
from engine.api import GameEngine
from engine.events import EVENT_NAMES


from tests.setup_support import finish_initial_daowen
def _engine(tmp_path, seed=5):
    engine = GameEngine(
        db_path=str(tmp_path / "rulings.db"), save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"), rng_seed=seed,
    )
    engine.execute_action("setup_attributes", {
        "name": "探索者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    engine.state.shards = 100
    engine.state.player.mana_limit = engine.state.player.current_mana = 100
    return engine


def _limit_pool(engine, names):
    all_names = set(EVENT_NAMES["通用"] + EVENT_NAMES["罪孽都市"])
    engine.event_pool.triggered = all_names - set(names)


def _resolve_current_safely(engine):
    name = engine.event_pool.current
    event = engine.event_pool.events[name]
    preferred = next((option for option in event["options"]
                      if any(word in option["text"] for word in
                             ("无事发生", "拒绝", "离开", "观棋", "视而不见"))), None)
    option = preferred or event["options"][-1]
    return engine.execute_action("resolve_event", {
        "event": name, "option_id": option["id"], "x": 1,
        "resonance_type": "转换", "daowen_names": ["杀伐"],
    })


def test_all_declared_first_tier_events_are_parsed():
    engine = GameEngine(rng_seed=1)
    declared = {name for names in EVENT_NAMES.values() for name in names}
    assert set(engine.event_pool.events) == declared
    assert len(declared) == 36  # 30 + 乱葬岗6事件
    assert "猩红暴雨" in engine.event_pool.events


def test_explore_tier_one_discovers_one_without_shard_cost(tmp_path):
    engine = _engine(tmp_path)
    _limit_pool(engine, ["祭坛"])
    before = (engine.state.shards, engine.state.energy)

    result = engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 1})

    assert result["success"]
    assert result["result"]["discovered_events"] == ["祭坛"]
    assert result["result"]["shard_cost"] == 0
    assert engine.state.shards == before[0] and engine.state.energy == before[1] - 1
    assert engine.event_pool.current == "祭坛" and engine.state.pending_event_queue == []


def test_explore_tier_two_costs_thirty_and_resolves_two_distinct_events_in_order(tmp_path):
    engine = _engine(tmp_path)
    _limit_pool(engine, ["祭坛", "猩红暴雨"])

    result = engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 2})

    assert result["success"]
    discovered = result["result"]["discovered_events"]
    assert len(discovered) == 2 and len(set(discovered)) == 2
    assert set(discovered) == {"祭坛", "猩红暴雨"}
    assert engine.state.shards == 70
    assert engine.event_pool.current == discovered[0]
    assert engine.state.pending_event_queue == [discovered[1]]

    first = _resolve_current_safely(engine)
    assert first["success"]
    assert engine.event_pool.current == discovered[1]
    assert engine.state.pending_event_queue == []
    assert first["result"]["next_event"]["event"] == discovered[1]

    second = _resolve_current_safely(engine)
    assert second["success"] and engine.event_pool.current is None
    assert second["completed_exploration"] is True


def test_pending_event_blocks_other_prebattle_actions_without_spending_energy(tmp_path):
    engine = _engine(tmp_path)
    _limit_pool(engine, ["祭坛"])
    engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 1})
    before = (engine.state.energy, engine.state.shards, engine.state.player.mana_limit)

    blocked = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1, "to": "mana",
    })

    assert not blocked["success"] and "尚未结算" in blocked["error"]
    assert (engine.state.energy, engine.state.shards, engine.state.player.mana_limit) == before
    available = engine.get_available_actions()
    assert available["phase"] == "事件待结算"
    assert all(action["action_type"] == "resolve_event" for action in available["actions"])


def test_explore_tier_two_illegal_inputs_are_atomic_and_do_not_consume_rng(tmp_path):
    engine = _engine(tmp_path)
    _limit_pool(engine, ["祭坛"])
    before = (engine.state.energy, engine.state.shards, len(engine.dice.get_history()))

    too_few = engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 2})
    assert not too_few["success"]
    assert (engine.state.energy, engine.state.shards, len(engine.dice.get_history())) == before

    invalid = engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 3})
    assert not invalid["success"]
    assert (engine.state.energy, engine.state.shards, len(engine.dice.get_history())) == before

    _limit_pool(engine, ["祭坛", "猩红暴雨"])
    engine.state.shards = 29
    no_money = engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 2})
    assert not no_money["success"]
    assert engine.state.shards == 29 and len(engine.dice.get_history()) == before[2]


def test_new_cycle_clears_triggered_and_pending_events(tmp_path):
    engine = _engine(tmp_path)
    engine.event_pool.triggered = {"祭坛"}
    engine.event_pool.current = "猩红暴雨"
    engine.state.pending_event_queue = ["祭坛"]
    engine.state.death_book_wisdom = ["癌变·甲"]

    engine._reset_after_death()

    assert engine.event_pool.triggered == set()
    assert engine.event_pool.current is None
    assert engine.state.pending_event_queue == []
    assert engine.state.death_book_wisdom == ["癌变·甲"]


def test_event_queue_survives_versioned_save(tmp_path):
    engine = _engine(tmp_path)
    _limit_pool(engine, ["祭坛", "猩红暴雨"])
    engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 2})
    current = engine.event_pool.current
    queued = list(engine.state.pending_event_queue)
    assert engine.save_game("event_queue")["version"] == engine.SAVE_FORMAT_VERSION
    engine.event_pool.current = None
    engine.state.pending_event_queue = []

    loaded = engine.load_game("event_queue")

    assert loaded["success"]
    assert engine.event_pool.current == current
    assert engine.state.pending_event_queue == queued
