"""【煌妖幡】／【造生】妖怪：多只、页数、休眠与主动唤醒。"""
import pickle

from engine.api import GameEngine
from engine.combat_events import CombatEventType
from engine.death_pages import bind_resources
from engine.models import Entity
from engine.world import WorldActorState, WorldState
from engine.yaoguai import BANNER_CAPACITY, BANNER_NAME, owned_yaoguai


def make_engine(tmp_path, *, banner=True, world=True):
    engine = GameEngine(db_path=str(tmp_path / "yaoguai.db"),
                        save_dir=str(tmp_path), rng_seed=730, autosave=False)
    engine.state.player = Entity(
        "P", "轮回者", blood_limit=60, current_hp=60,
        mana_limit=8, current_mana=8, speed_limit=6, current_speed=6,
        attack_count=4, attack_power=5,
    )
    engine.state.phase = "pre_battle"
    engine.state.current_region = "扭曲都市" if banner else "乱葬岗"
    engine.state.shards = 500
    if banner:
        engine.state.artifacts_owned = [BANNER_NAME]
    if world:
        assert engine.execute_action("world_initialize", {"width": 21, "height": 21})["success"]
        # 本组测试不消费随机事件；世界时钟、地图单位和行动原子性仍照常运行。
        engine.event_pool.events = {}
    return engine


def create(engine, name):
    return engine.execute_action("pre_battle_action", {
        "sub_action": "造生", "name": name,
    })


def make_yaoguai(engine, name, *, blood=40, dormant=False):
    being = Entity(name, "朋友", blood_limit=blood,
                   current_hp=0 if dormant else blood,
                   is_alive=not dormant)
    being.is_created_being = True
    being.created_by_runtime_id = engine.state.player.runtime_id
    being.is_dormant = dormant
    engine.state.friends.append(being)
    return being


def pages_used(engine):
    bind_resources(engine.state)
    return engine.state.player.pages_used


def test_banner_houses_six_without_extra_pages_and_rejects_seventh(tmp_path):
    engine = make_engine(tmp_path, banner=True)
    assert pages_used(engine) == 1  # 幡本身的一张法器页。

    for index in range(BANNER_CAPACITY):
        result = create(engine, f"妖{index + 1}")
        assert result["success"], result
        assert result["result"]["banner_housed"] is True
        assert pages_used(engine) == 1

    beings = owned_yaoguai(engine.state)
    assert len(beings) == BANNER_CAPACITY
    assert len({being.runtime_id for being in beings}) == BANNER_CAPACITY
    shards = engine.state.shards
    denied = create(engine, "第七只")
    assert not denied["success"] and "最多收纳6只" in denied["error"]
    assert len(owned_yaoguai(engine.state)) == BANNER_CAPACITY
    assert engine.state.shards == shards
    assert pages_used(engine) == 1


def test_unhoused_yaoguai_cost_five_pages_each_and_failed_creation_is_atomic(tmp_path):
    engine = make_engine(tmp_path, banner=False)
    assert create(engine, "甲")["success"]
    assert pages_used(engine) == 5
    assert create(engine, "乙")["success"]
    assert pages_used(engine) == 10

    shards = engine.state.shards
    denied = create(engine, "丙")
    assert not denied["success"] and "每只占5张" in denied["error"]
    assert [being.name for being in owned_yaoguai(engine.state)] == ["甲", "乙"]
    assert engine.state.shards == shards
    assert pages_used(engine) == 10

    # 取得幡后，已有两只也改为由幡承载；主人只支付幡的一页。
    engine.state.artifacts_owned.append(BANNER_NAME)
    assert pages_used(engine) == 1


def test_zaosheng_only_creates_and_generic_xiuxing_handles_growth(tmp_path):
    engine = make_engine(tmp_path, banner=True)
    assert create(engine, "甲")["success"]
    assert create(engine, "乙")["success"]
    first, second = owned_yaoguai(engine.state)
    before = (first.blood_limit, first.mana_limit, first.speed_limit,
              first.created_being_vision_range)
    second_before = (second.blood_limit, second.mana_limit, second.speed_limit)

    shards = engine.state.shards
    rejected = engine.execute_action("pre_battle_action", {
        "sub_action": "造生", "target": first.runtime_id,
    })
    assert not rejected["success"] and "造生仅用于创造" in rejected["error"]
    assert "修行、学习" in rejected["error"]
    assert engine.state.shards == shards
    assert len(owned_yaoguai(engine.state)) == 2
    assert (first.blood_limit, first.mana_limit, first.speed_limit,
            first.created_being_vision_range) == before

    # 用户裁定：既有妖怪的成长由通用行动承接，不再经造生重复实现。
    grown = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1, "target": first.name,
        "allocations": {"blood_points": 1, "speed_points": 0, "mana_points": 0},
    })
    assert grown["success"], grown
    assert first.blood_limit == before[0] + 6
    assert first.mana_limit == before[1] and first.speed_limit == before[2]
    assert first.created_being_vision_range == before[3]
    assert (second.blood_limit, second.mana_limit, second.speed_limit) == second_before
    assert len(owned_yaoguai(engine.state)) == 2


def test_hp_zero_is_dormancy_and_monster_blood_limit_is_shared_without_waking(tmp_path):
    engine = make_engine(tmp_path, banner=False, world=False)
    first = make_yaoguai(engine, "休眠甲", blood=40)
    second = make_yaoguai(engine, "休眠乙", blood=40, dormant=True)
    first.current_hp = 0
    first.is_alive = False

    assert engine.combat._check_hp_zero_death(first) is True
    assert first.is_dormant and not first.is_alive and first.current_hp == 0
    assert not hasattr(first, "_death_triggers_emitted")
    assert not any(event.event_type == CombatEventType.ENTITY_DIED
                   and event.target_name == first.name
                   for event in engine.state.combat_events)

    monster = Entity("血限25怪物", "怪物", blood_limit=25,
                     current_hp=0, is_alive=False)
    engine.state.enemies = [monster]
    assert engine.combat._check_hp_zero_death(monster) is True
    # 25点总池在两个休眠目标间整数平分，余数只影响其中一只。
    assert sorted([first.current_hp, second.current_hp]) == [12, 13]
    assert first.is_dormant and second.is_dormant
    assert not first.is_alive and not second.is_alive
    recovery = monster._death_ctx["yaoguai_recovery"]
    assert sum(item["heal_amount"] for item in recovery) == 25
    assert all(item["recovery_pool"] == 25 and item["dormant_count"] == 2
               for item in recovery)


def test_dormant_recovery_survives_battle_end_and_still_requires_wake(tmp_path):
    engine = make_engine(tmp_path, banner=False, world=False)
    dormant = make_yaoguai(engine, "休眠妖", blood=40, dormant=True)
    monster = Entity("血限18怪物", "怪物", blood_limit=18,
                     current_hp=0, is_alive=False)
    engine.state.enemies = [monster]
    engine.combat._check_hp_zero_death(monster)
    assert dormant.current_hp == 18 and dormant.healed_this_battle == 18

    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_battle = 1
    ended = engine.execute_action("battle_end", {})
    assert ended["success"], ended
    assert dormant.current_hp == 18
    assert dormant.is_dormant and not dormant.is_alive


def test_wake_is_an_explicit_targeted_action_and_requires_positive_hp(tmp_path):
    engine = make_engine(tmp_path, banner=True)
    assert create(engine, "待醒")["success"]
    being = owned_yaoguai(engine.state)[0]
    being.current_hp = 0
    being.is_alive = False
    being.is_dormant = True
    being.is_summoned = False
    engine.state.world_state["actors"][being.runtime_id]["alive"] = False

    denied = engine.execute_action("pre_battle_action", {
        "sub_action": "唤醒", "target": being.name,
    })
    assert not denied["success"] and "当前生命为0" in denied["error"]
    assert being.is_dormant and not being.is_alive

    being.current_hp = 7
    awakened = engine.execute_action("pre_battle_action", {
        "sub_action": "唤醒", "target": being.runtime_id,
    })
    assert awakened["success"], awakened
    assert awakened["result"]["current_hp"] == 7
    assert being.current_hp == 7 and being.is_alive and not being.is_dormant
    assert engine.state.world_state["actors"][being.runtime_id]["alive"] is True


def test_map_command_and_combat_summon_can_select_second_yaoguai(tmp_path):
    engine = make_engine(tmp_path, banner=True)
    assert create(engine, "甲")["success"]
    assert create(engine, "乙")["success"]
    first, second = owned_yaoguai(engine.state)

    commanded = engine.execute_action("world_command_created_being", {
        "behavior": "follow", "target": second.name,
    })
    assert commanded["success"], commanded
    world = WorldState.from_dict(engine.state.world_state)
    assert world.actors[second.runtime_id].behavior == "follow"
    assert world.actors[first.runtime_id].behavior == "explore"

    monster = Entity("遭遇怪", "怪物", blood_limit=12, current_hp=12)
    engine.state.enemies = [monster]
    player_actor = world.actors[engine.state.player.runtime_id]
    world.add_actor(WorldActorState(
        monster.runtime_id, monster.name, "enemy", *player_actor.position,
        faction="enemy", mode="autonomous",
    ))
    encounter = world.start_encounter(
        [engine.state.player.runtime_id, monster.runtime_id], player_actor.position)
    engine.state.world_state = world.to_dict()
    started = engine.execute_action("battle_start", {
        "world_encounter_id": encounter.encounter_id, "relic_choices": {},
        "summon_created_being": True,
        "summon_created_being_target": second.runtime_id,
    })
    assert started["success"], started
    assert second.is_summoned and not first.is_summoned
    assert second in engine.state.get_all_player_side()
    assert first not in engine.state.get_all_player_side()


def test_legacy_banner_name_migrates_and_dormancy_save_load_roundtrips(tmp_path):
    engine = make_engine(tmp_path, banner=False, world=False)
    engine.state.artifacts_owned = ["万魂幡"]
    assert "万魂幡" not in engine.DRAFT_ARTIFACTS and BANNER_NAME in engine.DRAFT_ARTIFACTS

    dormant = make_yaoguai(engine, "存档妖", dormant=True)
    dormant.current_hp = 9
    restored = pickle.loads(pickle.dumps(dormant))
    assert restored.is_dormant and not restored.is_alive

    assert engine.save_game("yaoguai-legacy")["success"]
    loaded_engine = GameEngine(db_path=str(tmp_path / "loaded.db"),
                               save_dir=str(tmp_path), rng_seed=731, autosave=False)
    loaded = loaded_engine.load_game("yaoguai-legacy")
    assert loaded["success"], loaded
    assert loaded_engine.state.artifacts_owned == [BANNER_NAME]
    loaded_being = owned_yaoguai(loaded_engine.state)[0]
    assert loaded_being.is_dormant and not loaded_being.is_alive
    assert loaded_being.current_hp == 9
    bind_resources(loaded_engine.state)
    assert loaded_engine.state.player.pages_used == 1
