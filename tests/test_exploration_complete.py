"""旧局外探索入口已退役；探索与事件发现由开放世界地图承载。"""
from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path):
    engine = GameEngine(
        db_path=str(tmp_path / "rulings.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=5,
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
    return engine


def test_all_declared_events_are_still_parsed():
    engine = GameEngine(rng_seed=1)
    assert len(engine.event_pool.events) == 36
    assert "猩红暴雨" in engine.event_pool.events


def test_legacy_prebattle_explore_is_rejected_without_mutation(tmp_path):
    engine = _engine(tmp_path)
    before = (engine.state.shards, engine.state.player.current_hp,
              engine.state.player.current_mana, len(engine.dice.get_history()))

    result = engine.execute_action("pre_battle_action", {
        "sub_action": "探索", "tier": 1,
    })

    assert not result["success"]
    assert "开放世界已删除独立探索行动" in result["error"]
    assert (engine.state.shards, engine.state.player.current_hp,
            engine.state.player.current_mana, len(engine.dice.get_history())) == before


def test_world_initialization_is_the_current_exploration_entry(tmp_path):
    engine = _engine(tmp_path)
    result = engine.execute_action("world_initialize", {"width": 7, "height": 7})
    assert result["success"], result
    assert engine.state.world_state["clock"]["turn"] == 0
    assert engine.state.world_state["map"]
