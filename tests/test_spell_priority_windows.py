"""交替优先权下的可暂停施法窗口专项。"""
from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine(*, player_extra=0):
    engine = GameEngine(autosave=False)
    player = Entity(
        "P", "轮回者", blood_limit=50, current_hp=50,
        mana_limit=100, current_mana=100, speed_limit=1, current_speed=1,
        attack_count=1, attack_power=1,
    )
    monster = Entity(
        "M", "怪物", blood_limit=50, current_hp=50,
        mana_limit=100, current_mana=100, speed_limit=1, current_speed=1,
        attack_count=1, attack_power=1,
    )
    for entity in (player, monster):
        for name in ("杀伐", "再生", "庇护"):
            entity.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula="", cost_type="消耗",
                       cost_formula="X", effect_formula=""),
                x_value=0, x_free=True,
            )
    if player_extra:
        player.add_status(StatusEffect(
            name="蓄锐·增", value=player_extra,
            remaining_rounds=1, source="测试夹具",
        ))
    state = engine.state
    state.player = player
    state.enemies = [monster]
    state.friends = []
    state.employees = []
    state.temp_friends = []
    state.phase = "in_combat"
    state.combat_subphase = "player_actions"
    state.current_round = 1
    state.duel_turn = "player_side"
    engine.combat._bind_existing_hp_hooks()
    return engine, player, monster


def _cast(engine, actor_ref, flow, target_ref, xs):
    return engine.execute_action("cast", {
        "actor_ref": actor_ref, "flow": flow, "target_ref": target_ref,
        "steps": [{"x": x} for x in xs],
    })


def _daowen(engine, actor_ref, target_ref, name="杀伐", x=1):
    return engine.execute_action("use_daowen", {
        "actor_ref": actor_ref, "daowen_name": name, "x": x,
        "target_ref": target_ref, "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {},
    })


def _skip(engine, actor_ref):
    return engine.execute_action("skip", {"actor_ref": actor_ref})


def _names(window):
    return [step["daowen"] for step in window["resolved_steps"]]


def test_three_step_spell_pays_each_step_and_has_final_window():
    engine, player, monster = _engine(player_extra=2)
    flow = "发动庇护X于自身→发动再生X于自身→发动杀伐X于目标"
    response = _cast(engine, "player:0", flow, "enemy:0", [1, 1, 1])
    assert response["success"], response
    assert response["result"]["execution_status"] == "awaiting_window"
    assert (player.actions_used_this_round, monster.actions_used_this_round) == (1, 0)
    assert _names(response["result"]["spell_window"]) == ["庇护"]

    rejected = _skip(engine, "player:0")
    assert rejected["success"] is False
    assert "当前轮到opponent_side" in rejected["error"]
    assert player.actions_used_this_round == 1

    counter = _daowen(engine, "enemy:0", "player:0")
    assert counter["success"], counter
    assert (player.actions_used_this_round, monster.actions_used_this_round) == (2, 1)
    assert _names(counter["spell_window"]) == ["庇护", "再生"]

    finished = _skip(engine, "enemy:0")
    assert finished["success"], finished
    # 【跳过】结束敌方回合；其后的空档自动让过，第三步与收尾连续完成。
    assert player.actions_used_this_round == 3
    assert finished["spell_progress"][-1]["execution_status"] == "completed"
    assert finished.get("spell_window") is None
    assert monster.current_hp == 49
    assert engine.combat._spell_stack == []
    # 最后的“施法结束”空档不预付、不补扣第四手；跳过也不扣敌方出手。
    assert (player.actions_used_this_round, monster.actions_used_this_round) == (3, 1)
    assert "opponent_side" in engine.combat._ended_turn_sides


def test_unpayable_next_step_interrupts_without_erasing_prior_step():
    engine, player, monster = _engine()
    flow = "发动杀伐X于目标→发动再生X于自身"
    response = _cast(engine, "player:0", flow, "enemy:0", [1, 1])
    assert response["success"], response
    assert monster.current_hp == 49
    assert player.actions_used_this_round == 1

    resumed = _skip(engine, "enemy:0")
    assert resumed["success"], resumed
    progress = resumed["spell_progress"][-1]
    assert progress["execution_status"] == "interrupted"
    assert progress["interrupt_reason"] == "action_insufficient"
    assert [step["status"] for step in progress["step_results"]] == [
        "completed", "interrupted",
    ]
    assert monster.current_hp == 49
    assert player.actions_used_this_round == 1
    assert engine.combat._spell_stack == []


def test_nested_counter_spell_finishes_before_outer_next_step():
    engine, player, monster = _engine(player_extra=2)
    outer = "发动庇护X于自身→发动再生X于自身→发动杀伐X于目标"
    assert _cast(engine, "player:0", outer, "enemy:0", [1, 1, 1])["success"]
    inner = _cast(engine, "enemy:0", "发动杀伐X于目标", "player:0", [1])
    assert inner["success"], inner
    assert len(engine.combat._spell_stack) == 2
    assert (player.actions_used_this_round, monster.actions_used_this_round) == (1, 1)

    # 玩家跳过内层最后空档：内层完成后，敌方这次“施法反制”行动才结束，
    # 因而同一次恢复推进外层第二步，而不是再要敌方额外skip一次。
    resumed = _skip(engine, "player:0")
    assert resumed["success"], resumed
    assert len(engine.combat._spell_stack) == 1
    assert player.actions_used_this_round == 2
    assert _names(resumed["spell_window"]) == ["庇护", "再生"]

    completed = _skip(engine, "enemy:0")
    assert completed["success"]
    assert player.actions_used_this_round == 3
    assert completed["spell_progress"][-1]["execution_status"] == "completed"
    assert completed.get("spell_window") is None
    assert engine.combat._spell_stack == []


def test_counter_kill_interrupts_caster_without_charging_dead_actor():
    engine, player, monster = _engine(player_extra=2)
    player.blood_limit = player.current_hp = 10
    monster.attack_power = 20
    flow = "发动再生X于自身→发动再生X于自身→发动再生X于自身"
    assert _cast(engine, "player:0", flow, "player:0", [1, 1, 1])["success"]

    prepared = engine.execute_action("prepare_attack", {"actor_ref": "enemy:0"})
    assert prepared["success"], prepared
    killed = engine.execute_action("resolve_attack", {
        "token": prepared["result"]["token"],
        "hits": [{
            "target_ref": "player:0", "damage": 20,
            "dodge": False, "parry": False, "blood_shadow": False,
            "spell_choices": {"before": {}, "after": {}},
        }],
    })
    assert killed["success"], killed
    assert not player.is_alive
    assert killed["spell_progress"][-1]["execution_status"] == "interrupted"
    assert killed["spell_progress"][-1]["interrupt_reason"] == "caster_dead"
    assert player.actions_used_this_round == 1
    assert monster.actions_used_this_round == 1
    assert engine.combat._spell_stack == []


def test_failed_counter_action_rolls_back_without_advancing_parent():
    engine, player, monster = _engine(player_extra=1)
    flow = "发动庇护X于自身→发动再生X于自身"
    assert _cast(engine, "player:0", flow, "enemy:0", [1, 1])["success"]
    window = engine._spell_window_payload()

    rejected = _daowen(engine, "enemy:0", "missing:9")
    assert rejected["success"] is False
    assert len(engine.combat._spell_stack) == 1
    assert engine._spell_window_payload()["resolved_steps"] == window["resolved_steps"]
    assert (player.actions_used_this_round, monster.actions_used_this_round) == (1, 0)

    resumed = _skip(engine, "enemy:0")
    assert resumed["success"], resumed
    assert player.actions_used_this_round == 2
    assert resumed["spell_progress"][-1]["execution_status"] == "completed"
    assert resumed.get("spell_window") is None


def test_failure_after_parent_resume_restores_old_window(monkeypatch):
    engine, player, monster = _engine(player_extra=1)
    flow = "发动庇护X于自身→发动杀伐X于目标"
    assert _cast(engine, "player:0", flow, "enemy:0", [1, 1])["success"]
    monkeypatch.setattr("engine.death_pages.growth_error", lambda *_: "强制回滚")

    rejected = _skip(engine, "enemy:0")
    assert rejected["success"] is False
    assert rejected["error"] == "强制回滚"
    assert player.actions_used_this_round == 1
    assert monster.current_hp == 50
    assert len(engine.combat._spell_stack) == 1
    assert _names(engine._spell_window_payload()) == ["庇护"]
    assert engine.state.duel_turn == "opponent_side"

    monkeypatch.setattr("engine.death_pages.growth_error", lambda *_: None)
    resumed = _skip(engine, "enemy:0")
    assert resumed["success"], resumed
    assert player.actions_used_this_round == 2
    assert monster.current_hp == 49
    assert resumed["spell_progress"][-1]["execution_status"] == "completed"
    assert resumed.get("spell_window") is None


def test_paused_spell_save_load_replays_records_without_reapplying_effects(tmp_path):
    engine, player, monster = _engine(player_extra=2)
    engine.save_dir = str(tmp_path)
    flow = "发动杀伐X于目标→发动再生X于自身→发动杀伐X于目标"
    response = _cast(engine, "player:0", flow, "enemy:0", [1, 1, 1])
    assert response["success"], response
    assert monster.current_hp == 49
    assert player.actions_used_this_round == 1
    saved = engine.save_game("paused")
    assert saved["success"], saved

    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    restored = loaded.load_game("paused")
    assert restored["success"], restored
    lp, lm = loaded.state.player, loaded.state.enemies[0]
    assert len(loaded.combat._spell_stack) == 1
    assert loaded.state.duel_turn == "opponent_side"
    assert lm.current_hp == 49  # 重放记录没有再次发动第一步杀伐
    assert lp.actions_used_this_round == 1

    finished = _skip(loaded, "enemy:0")
    assert finished["success"], finished
    assert lp.actions_used_this_round == 3
    assert lm.current_hp == 48
    assert finished["spell_progress"][-1]["execution_status"] == "completed"
    assert finished.get("spell_window") is None
    assert loaded.combat._spell_stack == []


def test_bizhong_blocks_counter_action_but_not_skip_or_own_continuation():
    engine, player, monster = _engine(player_extra=1)
    monster.add_status(StatusEffect(
        name="必中", value=1, remaining_rounds=2, source="测试夹具",
    ))
    flow = "发动庇护X于自身→发动再生X于自身"
    assert _cast(engine, "player:0", flow, "enemy:0", [1, 1])["success"]

    rejected = _daowen(engine, "enemy:0", "player:0")
    assert rejected["success"] is False
    assert "【必中】封锁回合外行动" in rejected["error"]
    assert monster.actions_used_this_round == 0

    resumed = _skip(engine, "enemy:0")
    assert resumed["success"], resumed
    assert player.actions_used_this_round == 2
    assert resumed["spell_progress"][-1]["execution_status"] == "completed"
    assert resumed.get("spell_window") is None
