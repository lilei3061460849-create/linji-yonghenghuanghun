"""可玩网页界面的服务端回归测试。

界面层只做显示与转发，这里验证三件事：
1. 开局快照不提前泄漏迷雾外地形，并把同伴列为可建议对象；
2. 玩家提交的建议走真实引擎接口，采纳/拒绝都留下结果；
3. 战斗面板用的两个便捷入口（逐击攻击、敌方阶段）能产生合法提交。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from webapp import server as web
from engine.world import WorldState


@pytest.fixture(autouse=True)
def clean_session():
    web.SESSION["engine"] = None
    web.SESSION["log"] = []
    web.SESSION["last_world"] = None
    yield
    web.SESSION["engine"] = None
    web.SESSION["last_world"] = None


def _battle_engine(tmp_path):
    """在 7×7 空地图上布置一个相邻敌人，并走完 world_initialize。"""
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "webapp-battle.db"),
                        save_dir=str(tmp_path), rng_seed=83, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=40, current_hp=40,
                                 speed_limit=6, current_speed=6,
                                 mana_limit=8, current_mana=8,
                                 attack_count=3, attack_power=4)
    target = Entity("打手", "轮回者", blood_limit=60, current_hp=60,
                    speed_limit=4, current_speed=4,
                    attack_count=2, attack_power=2)
    engine.state.enemies = [target]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    target_id = target.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[target_id].x, world.actors[target_id].y = 2, 1
    world.actors[target_id].mode = "manual"
    engine.state.player.world_x, engine.state.player.world_y = 1, 1
    target.world_x, target.world_y = 2, 1
    engine.state.world_state = world.to_dict()
    web.SESSION["engine"] = engine
    return engine, player_id, target_id


def test_snapshot_keeps_fog_and_lists_allies():
    snapshot = web._start({"name": "界面测试", "region": "罪孽都市",
                           "blood_points": 11, "speed_points": 8, "mana_points": 6,
                           "width": 21, "height": 21, "demo_friend": True, "seed": 21})
    world = snapshot["world"]
    assert snapshot["phase"] == "pre_battle"
    assert world["self"]["position"] == [1, 1]
    # 迷雾：只下发已知格，绝不把 21×21 全地形交给前端。
    assert 0 < len(world["known_terrain"]) < 21 * 21
    assert len(world["allies"]) == 1
    ally = world["allies"][0]
    assert ally["behavior"] == "follow"
    assert ally["owner_actor_id"] == world["self"]["actor_id"]

    suggestion = web._execute("world_suggest_ally", {
        "target_actor_id": ally["actor_id"], "suggestion": "explore",
    })
    assert suggestion["success"], suggestion
    result = suggestion["result"]
    assert 5 <= result["chance_percent"] <= 95
    assert result["behavior"] in {"follow", "guard", "explore"}
    assert any(entry["type"].startswith("ally_suggestion")
               for entry in web._snapshot(web.SESSION["engine"])["world"]["facts"])


def test_battle_panel_attack_and_monster_phase_use_real_engine_rules(tmp_path):
    engine, player_id, target_id = _battle_engine(tmp_path)
    attacked = web._execute("world_interact", {"target_actor_id": target_id,
                                               "choice": "attack"})
    assert attacked["success"], attacked
    encounter_id = attacked["result"]["encounter_id"]
    assert web._execute("battle_start", {"world_encounter_id": encounter_id,
                                         "relic_choices": {}})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]

    # 未准备攻击时，便捷入口必须拒绝，而不是凭空结算。
    assert not web._ui_attack({"damages": [1]})["success"]

    prepared = web._execute("prepare_attack", {"actor_ref": "player:0"})
    assert prepared["success"], prepared
    snapshot = web._snapshot(engine)
    pending = snapshot["battle"]["pending_attack"]
    assert pending["hit_count"] >= 1
    before = snapshot["battle"]["combatants"][-1]["hp"]

    damages = [pending["damage_budget"] // max(1, pending["hit_count"])] * pending["hit_count"]
    resolved = web._ui_attack({"target_ref": pending["target_options"][0]["ref"],
                               "damages": damages})
    assert resolved["success"], resolved

    after_snapshot = web._snapshot(engine)
    after = after_snapshot["battle"]["combatants"][-1]["hp"]
    assert after < before

    # 敌方阶段：界面用 prepare + 合法提交助手结算，不能手搓非法结构。
    monster = web._ui_monster_phase({"dodge_refs": []})
    assert monster["success"], monster
    assert "player_hp" in monster["result"]


def test_snapshot_survives_pending_event_by_reusing_last_map(tmp_path):
    engine, player_id, _ = _battle_engine(tmp_path)
    first = web._snapshot(engine)
    assert first["world"]["self"]["actor_id"] == player_id

    engine.event_pool.current = "祭坛"
    blocked = web._snapshot(engine)
    assert blocked["world"]["stale"] is True
    assert blocked["world"]["self"]["actor_id"] == player_id
    assert blocked["event"]["name"] == "祭坛"
