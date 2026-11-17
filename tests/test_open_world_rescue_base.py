"""沉沦海潮下基地开放地图区域与休整加成回归测试。"""
from engine.api import GameEngine
from engine.models import Entity


def test_sunken_sea_world_generates_one_contiguous_three_by_three_rescue_base(tmp_path):
    engine = GameEngine(db_path=str(tmp_path / "rescue-base.db"), rng_seed=74)
    engine.state.player = Entity("测试者", "轮回者", blood_limit=100, current_hp=100)
    engine.state.current_region = "沉沦海"
    engine.state.phase = "pre_battle"

    result = engine.execute_action("world_initialize", {"width": 21, "height": 21})
    assert result["success"], result

    locations = [
        value for value in engine.state.world_state["map"]["locations"].values()
        if value.get("kind") == "rest_area" and value.get("area_id") == "rescue_base"
    ]
    assert len(locations) == 9
    positions = {(item["x"], item["y"]) for item in locations}
    xs = {x for x, _ in positions}
    ys = {y for _, y in positions}
    assert len(xs) == len(ys) == 3
    assert positions == {(x, y) for x in xs for y in ys}


def test_rescue_base_adds_twelve_to_the_existing_rest_heal_pool(tmp_path):
    engine = GameEngine(db_path=str(tmp_path / "rescue-heal.db"), rng_seed=75)
    player = Entity("测试者", "轮回者", blood_limit=100, current_hp=0)
    engine.state.player = player
    engine.state.current_region = "沉沦海"
    engine.state.world_state = {
        "actors": {"player-runtime": {"kind": "player", "x": 8, "y": 8}},
        "map": {"locations": {
            f"rescue_base_{i}": {
                "kind": "rest_area", "area_id": "rescue_base",
                "x": x, "y": y,
            }
            for i, (x, y) in enumerate(
                ( (x, y) for y in range(7, 10) for x in range(7, 10) ), 1)
        }},
    }

    result = engine._pre_battle_xiuzheng({
        "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": 32}],
    })
    assert result["success"], result
    assert result["result"]["base_heal_amount"] == 20
    assert result["result"]["area_bonus"] == 12
    assert result["result"]["heal_amount"] == 32


def test_rescue_base_bonus_does_not_apply_outside_the_area_or_other_dungeons(tmp_path):
    engine = GameEngine(db_path=str(tmp_path / "rescue-boundary.db"), rng_seed=76)
    engine.state.player = Entity("测试者", "轮回者", blood_limit=100, current_hp=0)
    engine.state.current_region = "沉沦海"
    engine.state.world_state = {
        "actors": {"player-runtime": {"kind": "player", "x": 20, "y": 20}},
        "map": {"locations": {
            "rescue_base_1": {
                "kind": "rest_area", "area_id": "rescue_base", "x": 7, "y": 7,
            },
        }},
    }

    outside = engine._pre_battle_xiuzheng({
        "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": 20}],
    })
    assert outside["success"], outside
    assert outside["result"]["area_bonus"] == 0
    assert outside["result"]["heal_amount"] == 20

    engine.state.current_region = "龙心谷"
    engine.state.world_state["actors"]["player-runtime"].update({"x": 7, "y": 7})
    other_region = engine._pre_battle_xiuzheng({
        "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": 20}],
    })
    assert other_region["success"], other_region
    assert other_region["result"]["area_bonus"] == 0
    assert other_region["result"]["heal_amount"] == 20
