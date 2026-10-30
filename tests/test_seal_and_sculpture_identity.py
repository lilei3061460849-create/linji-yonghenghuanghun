"""封印延迟回场；雕塑对任何非轮回者；轮回者开局攻面板 0×0。

封印X：支付异变X，使一个目标延后X回合再入场；暂离不是死亡或永久离场。

2026-10-08 用户令：封印的目标由"只限怪物"放开为**任意目标**（含[朋友]/[员工]/
造物/敌对轮回者，以及施法者自己）。回场一律回原阵营原下标。
暂离不是死亡或永久离场。
雕塑：用户裁定对任何非轮回者（怪物/微光者/赤族等）；轮回者不触发。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState
from tests.monster_phase_support import resolve_monster_phase


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_seal_sculp_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.state.current_region = "龙心谷"
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "await_round_start"
    p = engine.state.player
    p.dao_wen["封印"] = DaoWenInstance(
        DaoWen(name="封印", formula="", cost_type="消耗", cost_formula="10X", effect_formula=""))
    p.speed_limit = 12
    p.current_speed = 12
    p.current_mana = 40
    p.mana_limit = 40
    return engine


def _monster(name, hp=80, atk=4, power=6):
    return Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                  attack_count=atk, attack_power=power)


# ========================================================================
# 封印
# ========================================================================

def test_seal_delays_one_monster_and_reenters_on_scheduled_round():
    """封印X=2支付异变2，只让一个目标暂离，并在R+X回始回场。"""
    engine = _engine("seal_happy")
    a = _monster("怪甲")
    b = _monster("怪乙")
    engine.state.enemies.extend([a, b])
    engine.execute_action("round_start", {})  # R1
    mana = engine.state.player.current_mana
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 2, "target": "怪甲"})
    assert r["success"], r
    assert engine.state.player.current_mana == mana  # 封印是异变代价，不消耗法力
    assert engine.state.player.mutation_count == 2
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["target"] == "怪甲"
    assert seal["delay_rounds"] == 2
    assert seal["return_round"] == 3
    assert a.is_alive and not a.is_departed and not a.removed_without_kill
    assert a not in engine.state.enemies
    assert engine.state.delayed_monster_reentries[0]["monster"] is a
    assert b in engine.state.enemies and b.is_alive
    assert not engine.state.battle_won(), "暂离怪物仍阻塞战斗胜利"

    # 结束R1、进入R2：尚未回场。
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    r2 = engine.execute_action("round_start", {})
    assert engine.state.current_round == 2
    assert a not in engine.state.enemies
    assert not any(e.get("type") == "seal_reentry" for e in r2["result"]["effects"])

    # 进入R3回始：原怪物回场，当回合白板标记由 spawned_round 提供。
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    r3 = engine.execute_action("round_start", {})
    assert engine.state.current_round == 3
    assert a in engine.state.enemies and a.is_alive
    assert engine.state.delayed_monster_reentries == []
    assert any(e.get("type") == "seal_reentry" and e["entity"] == "怪甲"
               for e in r3["result"]["effects"])


def test_seal_accepts_any_living_on_field_target():
    """2026-10-08 用户令：封印放开到任意目标，敌对轮回者同样可被暂离。

    旧行为是「目标必须是当前在场的怪物」，敌对轮回者被拒；现在只要活着、在场上、
    没在暂离中即可，且回场必须回到原阵营（enemies 的原下标）。
    """
    engine = _engine("seal_target_type")
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=1, attack_power=1)
    other = _monster("占位怪")
    engine.state.enemies.extend([foe, other])
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": foe.name})
    assert r["success"], r
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["target"] == "敌对轮回者"
    assert seal["home"] == "enemies"
    assert engine.state.player.mutation_count == 1
    assert foe.is_alive and foe not in engine.state.enemies
    assert engine.state.delayed_monster_reentries[0]["monster"] is foe


def test_seal_on_a_friend_returns_them_to_the_friend_slot_not_enemies():
    """封印自己的[朋友]：脱场后必须回到 friends 原下标，绝不能被塞进 enemies。

    这是"放开到任意目标"最危险的回归——旧实现一律 `enemies.append(...)` 回场，
    封印队友会直接把队友变成敌人。
    """
    engine = _engine("seal_friend")
    f0 = Entity(name="甲友", entity_type="微光者", blood_limit=30, current_hp=30,
                attack_count=0, attack_power=0)
    f1 = Entity(name="乙友", entity_type="微光者", blood_limit=30, current_hp=30,
                attack_count=0, attack_power=0)
    engine.state.friends.extend([f0, f1])
    engine.execute_action("round_start", {})  # R1
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": "甲友"})
    assert r["success"], r
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["home"] == "friends" and seal["index"] == 0
    assert f0 not in engine.state.friends and f1 in engine.state.friends
    assert f0 not in engine.state.enemies, "暂离的朋友不能落进敌方列表"
    assert engine.state.is_sealed_away(f0)

    # 回场：回到 friends 的 0 号位（乙友之前），不是追加到末尾、更不是 enemies。
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})  # R2
    assert engine.state.friends == [f0, f1], engine.state.friends
    assert f0 not in engine.state.enemies
    assert not engine.state.is_sealed_away(f0)
    assert "friend:0" in engine.combat._combat_entity_refs()


def test_seal_on_self_removes_the_player_from_play_without_counting_as_a_loss():
    """封印施法者自己：暂离期间不在场上，但不是命零、不判负，X回合后原样回场。

    玩家是 state.player 单字段，摘不走，只能靠 `_delayed_by_seal` 标记表达
    "不在场上"——因此所有枚举入口（_combat_entity_refs / get_all_player_side）
    都必须尊重这个标记，否则封印自己会变成"无敌但还能行动"。
    """
    engine = _engine("seal_self")
    engine.execute_action("round_start", {})  # R1
    player = engine.state.player
    foe = _monster("对手怪")
    engine.state.enemies.append(foe)

    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 2,
                                             "target": player.name})
    assert r["success"], r
    assert engine.state.is_sealed_away(player)
    assert player.is_alive, "暂离不是命零"
    assert not engine.state.battle_lost(), "暂离不是判负"
    assert not engine.state.battle_won(), "暂离队列非空，仍阻塞战终"

    # 不在场上：既不能当目标，也不能当行动者。
    refs = engine.combat._combat_entity_refs()
    assert "player:0" not in refs, "暂离的玩家不能被选为目标"
    assert player not in engine.state.get_all_player_side(), "暂离的玩家不在场上"
    assert not engine.combat.can_act(player), "暂离的玩家不能出手"
    assert engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1,
                                                "target": "对手怪"})["success"] is False, \
        "被自己封印后不能再靠默认 actor=player 绕开校验发动道纹"

    # R2 尚未回场；R3 回始回到玩家位。
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    r2 = engine.execute_action("round_start", {})
    assert engine.state.current_round == 2
    assert engine.state.is_sealed_away(player)
    assert not any(e.get("type") == "seal_reentry" for e in r2["result"]["effects"])
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    r3 = engine.execute_action("round_start", {})
    assert engine.state.current_round == 3
    assert not engine.state.is_sealed_away(player)
    assert engine.state.player is player
    assert "player:0" in engine.combat._combat_entity_refs()
    assert engine.combat.can_act(player)
    assert any(e.get("type") == "seal_reentry" and e["entity"] == player.name
               for e in r3["result"]["effects"])
    assert engine.state.delayed_monster_reentries == []


def test_seal_still_refuses_units_that_are_not_on_the_field():
    """放开到任意目标不等于放开到任意对象：已离场/已撤退/已在暂离中的不能封印。"""
    engine = _engine("seal_refuse")
    engine.execute_action("round_start", {})
    gone = _monster("已离场怪")
    gone.depart_battle("逃跑")
    engine.state.enemies.append(gone)
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": gone.name})
    assert not r["success"], r
    assert "离场" in r["error"], r["error"]
    assert engine.state.player.mutation_count == 0, "被拒不能付代价"


def test_seal_delayed_monster_is_not_an_alt_victory_or_shardless_removal():
    """延迟中的怪物不写旧版封印离场字段；回场后命零走正常碎片路径。"""
    engine = _engine("seal_death")
    monster = _monster("回场怪", hp=80)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": monster.name})
    assert r["success"], r
    assert engine.state.player.mutation_count == 1
    assert not engine.state.battle_won()
    assert engine.execute_action("battle_end", {})["success"] is False

    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    monster = next(e for e in engine.state.enemies if e.name == "回场怪")
    monster.current_hp = 0
    monster.is_alive = False
    end = engine.execute_action("battle_end", {})
    assert end["success"], end
    assert end["result"]["removed_via_alt_path"] == []
    assert end["result"]["death_shard_rewards"]


def test_monster_preselected_attack_skips_target_sealed_by_its_own_daowen():
    """怪物先封印玩家后，其在 prepare 时预选的玩家普攻跳过而非整阶段失败。"""
    engine = _engine("seal_phase_target_drift")
    monster = _monster("石背熊", hp=42, atk=1, power=7)
    monster.dao_wen["封印"] = DaoWenInstance(
        DaoWen(name="封印", formula="", cost_type="异变", cost_formula="X",
               effect_formula=""), x_value=1, x_free=False)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    hp_before = engine.state.player.current_hp

    results = resolve_monster_phase(
        engine.combat, {"石背熊": "封印"}, target_refs={"石背熊": "player:0"})

    assert any(entry.get("daowen_activated") == "封印" for entry in results)
    skipped = [entry for entry in results if entry.get("skipped") == "预选目标已不可选中"]
    assert len(skipped) == 1
    assert skipped[0]["attacker"] == "石背熊" and skipped[0]["target"] == "贾凡"
    assert engine.state.is_sealed_away(engine.state.player)
    assert engine.state.player.current_hp == hp_before


# ========================================================================
# 雕塑
# ========================================================================

def test_setup_reincarnator_attack_panel_derives_from_speed_and_mana():
    """DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力（原「初始1×1面板」口径废止）。

    夹具把当前速度设为12、当前法力设为40，因此攻次/攻力应分别为 12/40。
    雕塑**不再**排除轮回者（见下两条）。
    """
    engine = _engine("one_atk")
    p = engine.state.player
    assert p.effective_attack_count() == p.current_speed == 12
    assert p.effective_attack_power() == p.current_mana == 40
    # DM裁定 2026-09-09：轮回者既有攻击力，雕塑不再排除轮回者
    assert engine.combat._can_be_sculptured(p) is True


def test_sculpture_monster_and_weiguang_on_both_sides():
    """正常路径：怪物与己方微光者攻力归 0 都化为雕塑。"""
    state = GameState()
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_count/attack_power
    # 面板无效；不给当前速度/法力的话玩家自己就是 0×0，会先被雕塑、污染计数断言。
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                          speed_limit=1, current_speed=1, mana_limit=1, current_mana=1)
    # DM裁定 2026-09-10：雕塑触发条件为攻次与攻力**都**为0（原「攻力归0即可」废止）
    m = _monster("石像鬼", hp=100, atk=0, power=0)
    friend = Entity(name="岩行者", entity_type="朋友", blood_limit=40, current_hp=40,
                    attack_count=0, attack_power=0, is_deployed=True)
    state.enemies.append(m)
    state.friends.append(friend)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    kinds = [p["type"] for p in paths]
    assert kinds.count("sculpture") == 2
    assert m.is_sculptured and not m.is_alive
    assert friend.is_sculptured and not friend.is_alive
    names = {c.name for c in state.consumables if c.kind == "sculpture"}
    assert names == {"石像鬼雕塑", "岩行者雕塑"}


def test_sculpture_employee_and_temp_friend_zero_count():
    """边界：攻次与攻力**都**归 0 才触发（DM裁定 2026-09-10）；仍留 3×1 的微光者不触发。"""
    state = GameState()
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_count/attack_power
    # 面板无效；不给当前速度/法力的话玩家自己就是 0×0，会先被雕塑、污染计数断言。
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                          speed_limit=1, current_speed=1, mana_limit=1, current_mana=1)
    emp = Entity(name="打手", entity_type="员工", blood_limit=48, current_hp=48,
                 attack_count=0, attack_power=0, is_deployed=True)
    temp = Entity(name="路人", entity_type="造物", blood_limit=20, current_hp=20,
                  attack_count=0, attack_power=0)
    ok = Entity(name="力士", entity_type="朋友", blood_limit=30, current_hp=30,
                attack_count=3, attack_power=1, is_deployed=True)
    state.employees.append(emp)
    state.temp_friends.append(temp)
    state.friends.append(ok)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert sum(1 for p in paths if p["type"] == "sculpture") == 2
    assert emp.is_sculptured and temp.is_sculptured
    assert ok.is_alive and not ok.is_sculptured


def test_sculpture_includes_chizu_and_reincarnator():
    """DM裁定 2026-09-09：赤族攻力归 0 雕塑；**双方轮回者 0×0 同样雕塑**。

    旧口径下这条断言是「只 1 座雕塑、双方轮回者存活」；裁定后 0×0 的轮回者
    与怪物同理，攻次/攻力归 0 即失去攻击手段 → 化为雕塑。
    """
    state = GameState()
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                    attack_count=0, attack_power=0)
    state.player = player
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=0, attack_power=0)
    chizu = Entity(name="赤仆", entity_type="赤族", blood_limit=30, current_hp=30,
                   attack_count=0, attack_power=0)
    state.enemies.append(foe)
    state.friends.append(chizu)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert sum(1 for p in paths if p["type"] == "sculpture") == 3
    assert chizu.is_sculptured and not chizu.is_alive
    assert player.is_sculptured and not player.is_alive
    assert foe.is_sculptured and not foe.is_alive
    names = {c.name for c in state.consumables if c.kind == "sculpture"}
    assert names == {"赤仆雕塑", "贾凡雕塑", "敌对轮回者雕塑"}


def test_sculpture_now_includes_zero_attack_reincarnator():
    """DM裁定 2026-09-09：只剩轮回者时，攻次/攻力 0 同样产生雕塑（旧口径为不产生）。"""
    state = GameState()
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                    attack_count=0, attack_power=0)
    state.player = player
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=0, attack_power=0)
    state.enemies.append(foe)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert sum(1 for p in paths if p["type"] == "sculpture") == 2
    assert player.is_sculptured and foe.is_sculptured
    assert {c.name for c in state.consumables if c.kind == "sculpture"} == {
        "贾凡雕塑", "敌对轮回者雕塑"}
