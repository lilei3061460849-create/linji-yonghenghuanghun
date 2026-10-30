"""【沉溺】按目标中断普通/额外回合的 runtime 回归。"""
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import (
    REGION_EXCLUSIVE_DAOWEN, UNIMPLEMENTED_DAOWEN, learnability_error,
)
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect


def _engine():
    engine = GameEngine(autosave=False)
    player = Entity(
        "轮回者", "轮回者", blood_limit=200, current_hp=200,
        mana_limit=200, current_mana=200, speed_limit=4, current_speed=4,
        attack_count=2, attack_power=4,
    )
    monster = Entity(
        "靶怪", "怪物", blood_limit=100, current_hp=100,
        mana_limit=50, current_mana=50, speed_limit=2, current_speed=2,
        attack_count=1, attack_power=3,
    )
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 2
    return engine, player, monster


def _give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗",
               cost_formula="10X", effect_formula=""),
        x_value=0, x_free=True,
    )


def _cast(engine, name, x, target_ref):
    return engine.execute_action("use_daowen", {
        "daowen_name": name, "x": x, "target_ref": target_ref,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })


def test_cost_ownership_and_resonance_branch():
    engine, _, target = _engine()
    del engine
    calc = DaoWenEngine.resolve("沉溺", 2, target=target)
    assert calc["cost_type"] == "消耗" and calc["cost"] == 20
    assert calc["block_target_rounds"] == 2
    assert calc["duration"] == -1  # 次数存 value；不按普通回终自然递减

    try:
        DaoWenEngine.resolve("沉溺", 1)
    except ValueError as exc:
        assert "显式选择目标" in str(exc)
    else:
        raise AssertionError("沉溺必须拒绝缺省目标")

    assert "沉溺" in REGION_EXCLUSIVE_DAOWEN["沉沦海"]
    assert "沉溺" not in UNIMPLEMENTED_DAOWEN
    known = set(DaoWenEngine.list_all())
    assert learnability_error(
        "沉溺", owned={"大梦"}, region="沉沦海", known=known) is None
    assert "当前副本无法习得" in learnability_error(
        "沉溺", owned={"大梦"}, region="龙心谷", known=known)
    assert ResonanceEngine.find_transformation("大梦", "反转") == "沉溺"
    assert ResonanceEngine.find_transformation("沉溺", "反转") == "大梦"


def test_normal_round_skip_is_target_local_and_last_block_still_skips():
    engine, player, monster = _engine()
    player.preferences = ["永恒的偏爱"]  # 不得把次数型沉溺改成0次/失效状态
    _give(player, "沉溺")
    monster.add_status(StatusEffect("测试减益", 1, 1, "test"))
    player.add_status(StatusEffect("玩家状态", 1, 1, "test"))
    monster.deferred_damage = [{"amount": 5}]

    cast = _cast(engine, "沉溺", 1, "enemy:0")
    assert cast["success"] is True, cast
    assert player.current_mana == 190
    status = monster.get_status_effects("沉溺")[0]
    assert status.value == 1 and status.is_permanent
    assert status.effect_data["manual_round_countdown"] is True
    assert not engine.combat.chenni_round_blocked(monster)  # 当前回合不 retroactive 中断

    before_round = engine.state.current_round
    started = engine.combat.round_start()
    assert engine.state.current_round == before_round + 1
    assert any(effect.get("type") == "chenni_round_blocked"
               and effect.get("remaining") == 0
               for effect in started["effects"])
    assert not monster.has_status("沉溺")
    assert engine.combat.chenni_round_blocked(monster)
    assert not engine.combat.chenni_round_blocked(player)
    assert monster.deferred_damage == [{"amount": 5}]
    assert monster.current_hp == 100

    prepared = engine.combat.prepare_monster_phase()
    assert prepared["actors"] == []
    assert prepared["skipped"][0]["actor_ref"] == "enemy:0"

    engine.combat.round_end()
    # 被跳过者不递减状态、不推进凡庸；其他角色仍正常进入回终。
    assert monster.get_status_effects("测试减益")[0].remaining_rounds == 1
    assert monster.no_action_rounds == monster.no_damage_rounds == 0
    assert not player.has_status("玩家状态")
    assert player.no_action_rounds == player.no_damage_rounds == 1

    # 次数已在上一回始归零，但上一回合仍完整跳过；下一次回始才恢复。
    engine.combat.round_start()
    assert not engine.combat.chenni_round_blocked(monster)
    assert monster.current_hp == 95  # 延后伤害留到下一次真正经历的回始
    assert engine.combat.prepare_monster_phase()["actors"][0]["actor_ref"] == "enemy:0"
    engine.combat.round_end()
    assert not monster.has_status("测试减益")
    assert monster.no_action_rounds == monster.no_damage_rounds == 1


def test_sealed_away_round_neither_consumes_nor_restores_old_block_marker():
    engine, player, monster = _engine()
    _give(player, "沉溺")
    assert _cast(engine, "沉溺", 2, "enemy:0")["success"] is True
    engine.combat.round_start()
    assert monster.get_status_effects("沉溺")[0].value == 1
    assert engine.combat.chenni_round_blocked(monster)

    home, index = engine.state.detach_from_field(monster)
    engine.state.delayed_monster_reentries.append({
        "monster": monster, "home": home, "index": index,
        "return_round": engine.state.current_round + 1, "delay_rounds": 1,
    })
    # 暂离期间不进入回始，不消费剩余沉溺，也不把上一轮标记带回场。
    engine.combat.round_start()
    assert monster in engine.state.enemies
    assert monster.get_status_effects("沉溺")[0].value == 1
    assert not engine.combat.chenni_round_blocked(monster)

    # 下一次真正以在场身份进入回合时才消费最后一次。
    engine.combat.round_start()
    assert not monster.has_status("沉溺")
    assert engine.combat.chenni_round_blocked(monster)


def test_blocked_player_has_no_action_or_round_cooldown_tick():
    engine, player, monster = _engine()
    engine.state.relics = [Relic("守夜灯", "每经历一个回合获得当前世界回合数的法力")]
    player.add_status(StatusEffect("疯狂", -1, 2, "test"))  # 夹具给出定义2阶法术所需预算
    _give(player, "沉溺")
    _give(monster, "大梦")
    cast = _cast(engine, "沉溺", 1, "player:0")
    assert cast["success"] is True, cast
    _give(player, "封印")
    defined = engine.execute_action("define_spell", {"spell_name": "镇魔印"})
    assert defined["success"] is True, defined
    player.dao_wen["沉溺"].cooldown_rounds_remaining = 2
    monster.dao_wen["大梦"].cooldown_rounds_remaining = 2

    engine.state.combat_subphase = "await_round_start"
    started = engine.execute_action("round_start", {})
    assert started["success"] is True, started
    assert engine.combat.chenni_round_blocked(player)
    assert player.current_mana == 190  # 被中断的普通回合不触发守夜灯
    assert player.dao_wen["沉溺"].cooldown_rounds_remaining == 2
    assert monster.dao_wen["大梦"].cooldown_rounds_remaining == 1

    attack = engine.execute_action("prepare_attack", {})
    assert attack["success"] is False
    available = engine.get_available_actions()
    assert {action["action_type"] for action in available["actions"]} <= {
        "resolve_ally_phases", "prepare_monster_phase",
    }
    assert "沉溺" in available["chenni_restriction"]

    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"] is True, prepared
    assert prepared["result"]["spell_logs"] == []
    assert not engine.state.delayed_monster_reentries
    assert player.mutation_count == 0  # 自身回合被跳过，镇魔印不自动发动


def test_dameng_after_blocked_normal_round_is_independent():
    engine, player, _ = _engine()
    target = Entity("持灯轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=0, speed_limit=3, current_speed=3)
    engine.state.enemies = [target]
    engine.state.opponent_relics = [Relic("守夜灯", "每经历一个回合获得当前世界回合数的法力")]
    _give(player, "沉溺")
    _give(player, "大梦")
    assert _cast(engine, "沉溺", 1, "enemy:0")["success"] is True
    engine.combat.round_start()
    assert engine.combat.chenni_round_blocked(target)
    assert target.current_mana == 0

    result = _cast(engine, "大梦", 1, "enemy:0")
    assert result["success"] is True, result
    effects = result["execution"]["effects"]
    assert not any(isinstance(effect, dict)
                   and effect.get("type") == "chenni_round_blocked" for effect in effects)
    assert sum(isinstance(effect, dict)
               and effect.get("type") == "extra_target_round" for effect in effects) == 1
    assert target.current_mana == 3  # 大梦自己的回增不能继承普通回合的跳过标记
    assert target.no_action_rounds == 1
    assert engine.state.current_round == 3
    assert engine.combat.chenni_round_blocked(target)  # 原普通回终仍须跳过

    engine.combat.round_end()
    assert target.no_action_rounds == 1


def test_dameng_extra_rounds_consume_blocks_before_any_round_settlement():
    engine, player, _ = _engine()
    target = Entity(
        "持灯轮回者", "轮回者", blood_limit=100, current_hp=100,
        mana_limit=100, current_mana=0, speed_limit=3, current_speed=3,
    )
    engine.state.enemies = [target]
    engine.state.opponent_relics = [
        Relic("守夜灯", "每经历一个回合获得当前世界回合数的法力")]
    engine.state.current_round = 10
    _give(player, "沉溺")
    _give(player, "大梦")
    target.add_status(StatusEffect("测试减益", 1, 1, "test"))
    player.add_status(StatusEffect("玩家状态", 3, 3, "test"))

    assert _cast(engine, "沉溺", 2, "enemy:0")["success"] is True
    result = _cast(engine, "大梦", 3, "enemy:0")
    assert result["success"] is True, result
    effects = result["execution"]["effects"]
    assert len([effect for effect in effects if isinstance(effect, dict)
                and effect.get("type") == "chenni_round_blocked"]) == 2
    assert len([effect for effect in effects if isinstance(effect, dict)
                and effect.get("type") == "extra_target_round"]) == 1

    # 前两个额外回合完整中断；只有第三个触发守夜灯、凡庸和状态递减。
    assert engine.state.current_round == 10
    assert target.current_mana == 10
    assert target.no_action_rounds == target.no_damage_rounds == 1
    assert not target.has_status("沉溺")
    assert not target.has_status("测试减益")
    assert player.get_status_effects("玩家状态")[0].remaining_rounds == 3
