"""2026-11-01 静态排查报告 6 项缺陷的验收回归。

对应 docs/静态排查报告-2026-11-01-状态分类与上限.md：
修复1 沉溺阻断下回终不再崩；修复5a 庇护纯次数型；修复6 招魂无尸体不吞代价。
修复2/3/4 分别见 test_debt_overdraft / test_dianjin_cost+test_escape_and_diandang /
test_haoduo_duration。
"""
from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect
from tests.setup_support import opponent_passes


def _engine(round_no=2):
    engine = GameEngine(autosave=False)
    player = Entity("轮回者", "轮回者", blood_limit=200, current_hp=200,
                    mana_limit=200, current_mana=200, speed_limit=4, current_speed=4,
                    attack_count=2, attack_power=4)
    monster = Entity("靶怪", "怪物", blood_limit=100, current_hp=100,
                     mana_limit=50, current_mana=50, speed_limit=2, current_speed=2,
                     attack_count=1, attack_power=3)
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.temp_friends = []
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = round_no
    return engine, player, monster


def _give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗",
               cost_formula="10X", effect_formula=""),
        x_value=0, x_free=True)


def _cast(engine, name, x, target_ref="enemy:0", **extra):
    return engine.execute_action("use_daowen", {
        "daowen_name": name, "x": x, "target_ref": target_ref,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
        **extra})


# ---------------- 修复1：沉溺阻断下的回终不再 UnboundLocalError ----------------

def test_round_end_with_chenni_blocked_player_does_not_crash():
    engine, player, monster = _engine()
    _give(player, "沉溺")
    assert _cast(engine, "沉溺", 1, "player:0")["success"] is True
    engine.combat.round_start()          # 回始消费一次沉溺 → 本回合被中断
    assert engine.combat.chenni_round_blocked(player)
    engine.state.relics = [Relic("旧印", "")]
    engine.state.sealed_relics = {"旧印": 3}
    # 关键点：玩家本回合被沉溺阻断 → 走不到封印递减分支；旧实现在函数体内定义
    # expired_seals/expired_steals，这里必抛 UnboundLocalError。
    result = engine.combat.round_end()
    assert isinstance(result, dict), result
    # 被阻断的一方不递减，其他实体的封印/豪夺照常推进。
    assert engine.state.sealed_relics == {"旧印": 3}
    assert monster.current_hp == 100


def test_round_end_ticks_other_entities_while_player_blocked():
    engine, player, monster = _engine()
    _give(player, "沉溺")
    _cast(engine, "沉溺", 1, "player:0")
    engine.combat.round_start()
    monster.sealed_relics["靶怪遗物"] = 1
    engine.combat.round_end()
    assert monster.sealed_relics == {}, "被阻断的只是玩家，怪物侧封印仍应递减到期"


# ---------------- 修复5a：庇护是纯次数型 ----------------

def test_bihu_is_pure_charges_without_countdown():
    engine, player, monster = _engine()
    _give(player, "庇护")
    player.current_mana = 200
    result = _cast(engine, "庇护", 2, "player:0")
    assert result["success"], result
    status = player.get_status_effects("庇护")[0]
    assert status.value == 2 and status.remaining_rounds == -1, status.to_dict()


def test_bihu_charges_survive_round_end():
    engine, player, monster = _engine()
    _give(player, "庇护")
    _cast(engine, "庇护", 3, "player:0")
    engine.combat.round_end()
    engine.combat.round_end()
    status = player.get_status_effects("庇护")
    assert status and status[0].value == 3, "纯次数型不应随回合递减"


def test_huixiang_cannot_target_pure_charge_status_and_charges_no_fee():
    engine, player, monster = _engine()
    _give(player, "回响")
    _give(player, "庇护")
    assert _cast(engine, "庇护", 2, "enemy:0")["success"]   # 庇护挂在靶怪身上
    # 交替出手：交权给敌方【跳过】再取回出手权（本文件只测回响的候选过滤）。
    opponent_passes(engine)
    engine.state.combat_subphase = "player_actions"
    player.actions_used_this_round = 0
    mana_before = player.current_mana
    result = _cast(engine, "回响", 2, "enemy:0")
    assert result["success"] is False, result
    assert "没有可被【回响】延长的状态" in result["error"], result
    assert player.current_mana == mana_before, "发不动时不得扣 5X 法力"
    assert player.actions_used_this_round == 0


# ---------------- 修复6：招魂无尸体不许吞代价 ----------------

def test_zhaohun_without_corpse_is_refused_before_mana_payment():
    engine, player, monster = _engine()
    _give(player, "招魂")
    engine.state.dead_monsters = []
    mana_before = player.current_mana
    result = _cast(engine, "招魂", 1, "player:0")
    assert result["success"] is False, result
    assert "尸体" in result["error"], result
    assert player.current_mana == mana_before
    assert player.actions_used_this_round == 0
    assert engine.state.temp_friends == []


def test_zhaohun_with_corpse_revives_it():
    engine, player, monster = _engine()
    _give(player, "招魂")
    engine.state.dead_monsters = [monster]
    monster.current_hp = 0
    result = _cast(engine, "招魂", 1, "player:0")
    assert result["success"], result
    assert [e.name for e in engine.state.temp_friends] == ["靶怪（魂）"]


# ---------------- 修复4a：豪夺自选拒绝 ----------------

def test_haoduo_on_self_is_refused():
    engine, player, monster = _engine()
    _give(player, "豪夺")
    engine.state.shards = 50
    engine.state.relics = [Relic("自己的遗物", "")]
    result = _cast(engine, "豪夺", 1, "player:0")
    assert result["success"] is False, result
    assert "他人" in result["error"], result
    assert engine.state.shards == 50
