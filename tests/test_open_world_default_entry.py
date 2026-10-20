"""Every selected dungeon defaults to the engine-backed open-world entry."""
import pytest

from engine.ai_player import PlaceholderBackend
from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


@pytest.mark.parametrize(("region", "npc_count"), [
    ("罪孽都市", 2), ("扭曲都市", 2), ("龙心谷", 2), ("乱葬岗", 3),
])
def test_selected_region_defaults_to_open_map_and_populates_world(tmp_path, region, npc_count):
    engine = GameEngine(db_path=str(tmp_path / f"{region}.db"), rng_seed=20261020)
    setup = engine.execute_action("setup_attributes", {
        "name": "开放地图测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    assert setup["success"], setup
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]

    region_result = engine.execute_action("setup_choose_region", {"region": region})
    assert region_result["success"], region_result
    assert region_result["next_actions"] == ["world_initialize"]

    # Even when the legacy battle action is available, open-world initialization remains first.
    engine.state.energy = 0
    available = engine.get_available_actions()
    assert available["actions"][0]["action_type"] == "world_initialize"
    assert "默认副本入口" in available["actions"][0]["note"]
    linear_battle = next(action for action in available["actions"]
                         if action["action_type"] == "battle_start")
    assert "旧线性副本入口" in linear_battle["note"]

    decision = PlaceholderBackend().decide(
        {"state": engine.state.to_dict()}, available)
    assert decision.action_type == "world_initialize"
    assert decision.params == {"width": 21, "height": 21}

    initialized = engine.execute_action(decision.action_type, decision.params)
    assert initialized["success"], initialized
    assert initialized["result"]["spawned_reincarnators"] == npc_count
    assert initialized["result"]["spawned_monsters"] == 12
    world_actions = {item["action_type"] for item in engine.get_available_actions()["actions"]}
    assert {"world_move", "world_wait", "world_interact"} <= world_actions
