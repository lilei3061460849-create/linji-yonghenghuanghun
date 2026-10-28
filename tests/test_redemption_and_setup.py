"""开局发现 / 新残韵 / 第一杯（重做：回复与失去的生命翻倍） / 净化 / 救赎。

每项覆盖正常、边界、非法三类。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.gamedata import SHAFA_LOOP_DAOWEN, ORIGINAL_MONSTER_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity, Relic
from tests.setup_support import finish_initial_daowen


def _engine(suffix, seed=11):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    return GameEngine(db_path=f"/tmp/linji_tests/redemp_{suffix}.db", rng_seed=seed)


def _give(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


def _ready_combat(engine, region="罪孽都市"):
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    optional = {"回锋刀", "共罪"}
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    engine.execute_action("battle_start", {"relic_choices": {}})
    round_choices = {}
    if any(r.name == "回锋刀" for r in engine.state.relics):
        round_choices["回锋刀"] = {"enemy_index": 0}
    if any(r.name == "共罪" for r in engine.state.relics):
        round_choices["共罪"] = {"use": False}
    engine.execute_action("round_start", {"relic_choices": round_choices})
    return engine


# ---------- 开局发现 ----------

def test_initial_daowen_discovery_normal():
    engine = _engine("init_ok")
    r = engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    assert r["success"]
    # 新开局流程：先发现遗物3选1，再发现初始道纹。
    relic_choices = r["result"]["relic_choices"]
    assert len(relic_choices) == 3 and len(set(relic_choices)) == 3
    picked_relic = engine.execute_action("choose_discovered_relic",
                                         {"relic_name": relic_choices[0]})
    assert picked_relic["success"]
    choices = picked_relic["result"]["daowen_choices"]
    assert len(choices) == 3 and len(set(choices)) == 3
    assert set(choices) <= set(SHAFA_LOOP_DAOWEN)
    assert engine.state.player.dao_wen == {}
    picked = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": choices[1]})
    assert picked["success"]
    assert list(engine.state.player.dao_wen) == [choices[1]]
    assert engine.state.pending_initial_daowen_choices == []


def test_initial_daowen_discovery_boundary_three_unique_from_loop():
    engine = _engine("init_bound", seed=99)
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": engine.state.pending_relic_choices[0]})
    choices = engine.state.pending_initial_daowen_choices
    assert all(name in SHAFA_LOOP_DAOWEN for name in choices)
    rolls = [h for h in engine.dice.get_history()
             if str(h.get("pool_name", "")).startswith("initial_daowen_discovery_")]
    assert len(rolls) == 3


def test_initial_daowen_discovery_rejects_illegal():
    engine = _engine("init_bad")
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    # 遗物未选择前，初始道纹与残韵均被拒绝
    early = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "杀伐"})
    assert not early["success"]
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": engine.state.pending_relic_choices[0]})
    choices = list(engine.state.pending_initial_daowen_choices)
    bad = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "净化"})
    assert not bad["success"]
    assert engine.state.player.dao_wen == {}
    assert engine.state.pending_initial_daowen_choices == choices
    resonance = engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    assert not resonance["success"]


# ---------- 残韵：永久改写 + 施法者同时获得 ----------

def test_resonance_permanently_converts_and_grants():
    engine = _ready_combat(_engine("res_ok"))
    engine.state.resonance["反转"] = 1
    _give(engine.state.player, "杀伐")
    monster = Entity(name="慢怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=2, attack_power=4)
    # 使用仍可获得的【必中】反转为【蒙蔽】，同时验证怪物其余原始道纹保留。
    _give(monster, "必中")
    _give(monster, "全力")
    monster._had_monster_daowen = True
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_resonance", {
        "source_daowen": "必中", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"]
    assert "必中" not in monster.dao_wen and "蒙蔽" in monster.dao_wen
    assert "全力" in monster.dao_wen
    assert "蒙蔽" in engine.state.player.dao_wen
    assert "蒙蔽" not in ORIGINAL_MONSTER_DAOWEN
    assert not engine.state.pending_redemption

def test_resonance_does_not_duplicate_same_name():
    engine = _ready_combat(_engine("res_dup"))
    engine.state.resonance["反转"] = 1
    _give(engine.state.player, "杀伐")
    _give(engine.state.player, "再生")
    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转", "target_daowen": "再生",
    })
    assert r["success"]
    assert list(engine.state.player.dao_wen).count("再生") == 1
    assert "杀伐" not in engine.state.player.dao_wen


def test_resonance_refuses_missing_stock_and_grants_original():
    """残韵库存不足时拒绝；授予不再区分原始/转化（2026-10-07 用户令：完全放开）。

    旧断言「原始怪物道纹不授予施法者」编码的是已被用户令删除的门禁，
    本用例按新裁定重写。
    """
    engine = _ready_combat(_engine("res_bad"))
    _give(engine.state.player, "杀伐")
    engine.state.resonance["反转"] = 0
    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转", "target_daowen": "再生",
    })
    assert not r["success"]
    assert "杀伐" in engine.state.player.dao_wen
    granted = engine._grant_transformed_daowen(engine.state.player, "疯狂")
    assert granted is True
    assert "疯狂" in engine.state.player.dao_wen


# ---------- 第一杯（2026-09-23 重做：回复与失去的生命翻倍） ----------

def test_first_cup_no_longer_blocks_player_cancer():
    engine = _ready_combat(_engine("bag_ok"))
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    player = engine.state.player
    player.total_healed = engine.combat.cancer_threshold_of(player)
    assert engine.combat.check_cancer(player) is not None, "旧免疫条款已废止"
    assert not player.is_alive


def test_moneybag_threshold_exactly_two_times_blood_limit():
    engine = _ready_combat(_engine("bag_th"))
    player = engine.state.player
    assert engine.combat.cancer_threshold_of(player) == player.blood_limit * 2
    player.total_healed = player.blood_limit * 2 - 1
    assert engine.combat.check_cancer(player) is None
    player.total_healed = player.blood_limit * 2
    hit = engine.combat.check_cancer(player)
    assert hit is not None and not player.is_alive


def test_first_cup_doubling_does_not_apply_to_friends():
    engine = _ready_combat(_engine("bag_ally"))
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    friend = Entity(name="同伴", entity_type="朋友", blood_limit=30, current_hp=10)
    engine.state.friends.append(friend)
    detail = engine.state.apply_heal(friend, 5)
    assert detail["heal_amount"] == 5 and friend.current_hp == 15, "朋友不继承倍率"
    friend.total_healed = engine.combat.cancer_threshold_of(friend)
    hit = engine.combat.check_cancer(friend)
    assert hit is not None
    assert friend.is_proliferated


# ---------- 净化 ----------

def test_jinghua_reduces_mutation():
    engine = _ready_combat(_engine("jh_ok"))
    player = engine.state.player
    _give(player, "净化")
    player.current_mana = 150
    monster = Entity(name="疫怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=2, attack_power=4)
    monster.mutation_count = 12
    _give(monster, "全力")
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 5, "target_ref": "enemy:0",
        "cost_choices": [{"type": "异变"}], "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"]
    assert monster.mutation_count == 7
    assert r["calculation"]["cost"] == 100  # 最新裁定：法力20X，X=5
    assert player.current_mana == 50


def test_jinghua_rejects_unowned_mutation():
    engine = _ready_combat(_engine("jh_neg"))
    player = engine.state.player
    _give(player, "净化")
    player.current_mana = 200
    player.speed_limit = 30
    monster = Entity(name="净怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=2, attack_power=4)
    monster.mutation_count = 0
    _give(monster, "全力")
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 30, "target_ref": "enemy:0",
        "cost_choices": [{"type": "异变"}], "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert not r["success"]
    assert monster.mutation_count == 0
    assert not engine.state.pending_redemption
    assert monster.is_alive


def test_jinghua_rejects_non_positive_x():
    """X=0 是合法的"拒绝发动"（DM裁定2026-08-30）：成功但跳过，不改异变。

    仍然被拒的只有负数/非整数 X。
    """
    engine = _ready_combat(_engine("jh_bad"))
    _give(engine.state.player, "净化")
    engine.state.player.current_mana = 20
    monster = Entity(name="靶", entity_type="怪物", blood_limit=40, current_hp=40)
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 0, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"] is True
    assert r.get("skipped") is True
    assert engine.state.player.current_mana == 20
    assert monster.mutation_count == 0


# ---------- 救赎 ----------

def test_redemption_fires_at_low_hp_even_with_transform_left():
    """正常：残血且卸掉最后一张原始道纹，转化道纹留下也触发救赎。"""
    engine = _ready_combat(_engine("rd_path"))
    engine.state.resonance["反转"] = 1
    monster = Entity(name="脑蜘蛛", entity_type="怪物", blood_limit=204, current_hp=20,
                     attack_count=2, attack_power=11)
    # 2026-10-11 用户令：【借力】随 A14 删除，【全力】的（转换）出边按用户口径**留空**
    # 已遗物化的【洞察】不可获得；本用例改用【必中】反转为【蒙蔽】。
    _give(monster, "必中")
    _give(monster, "坏死")
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_resonance", {
        "source_daowen": "必中", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"]
    assert r.get("redemption")
    assert engine.state.pending_redemption
    assert "蒙蔽" in engine.state.pending_redemption["dao_wen"]
    assert "必中" not in engine.state.pending_redemption["dao_wen"]
    assert not monster.is_alive
    accept = engine.execute_action("resolve_redemption", {"option": "接纳", "name": "微光蛛"})
    assert accept["success"]
    emp = next(e for e in engine.state.employees if e.name == "微光蛛")
    assert emp.entity_type == "员工" and emp.is_deployed is False
    assert emp.dao_wen == {}
    assert emp.blood_limit == math.ceil(204 / 2)


def test_redemption_skips_full_hp_or_remaining_original():
    """边界：满血无原始道纹不救赎；残血仍持有原始道纹也不救赎。"""
    engine = _ready_combat(_engine("rd_blank"))
    exclusive = Entity(name="血肉巨囊", entity_type="怪物", blood_limit=258, current_hp=258,
                       attack_count=1, attack_power=8)
    _give(exclusive, "爆裂")
    _give(exclusive, "增殖")
    engine.state.enemies[:] = [exclusive]
    assert engine.combat.check_redemption(exclusive) is None
    wounded = Entity(name="脑蜘蛛", entity_type="怪物", blood_limit=204, current_hp=20,
                     attack_count=2, attack_power=11)
    _give(wounded, "全力")
    engine.state.enemies[:] = [wounded]
    assert engine.combat.check_redemption(wounded) is None


def test_redemption_accept_creates_halved_employee_on_standby():
    engine = _ready_combat(_engine("rd_ok"))
    monster = Entity(name="悔怪", entity_type="怪物", blood_limit=81, current_hp=8,
                     attack_count=3, attack_power=5)
    engine.state.enemies[:] = [monster]
    engine.combat.check_redemption(monster)
    assert engine.state.pending_redemption
    blocked = engine.execute_action("round_end", {})
    assert not blocked["success"] and "救赎" in blocked["error"]
    r = engine.execute_action("resolve_redemption", {"option": 1, "name": "微光阿清"})
    assert r["success"]
    emp = next(e for e in engine.state.employees if e.name == "微光阿清")
    assert emp.entity_type == "员工" and emp.is_deployed is False
    assert emp.blood_limit == math.ceil(81 / 2)
    assert emp.attack_count == math.ceil(3 / 2)
    assert emp.attack_power == math.ceil(5 / 2)
    assert emp.dao_wen == {}
    assert not engine.state.pending_redemption
    assert engine.state.friends == []


def test_redemption_ignore_and_no_false_positive():
    engine = _ready_combat(_engine("rd_ig"))
    exclusive = Entity(name="塔中影", entity_type="怪物", blood_limit=60, current_hp=60,
                       attack_count=2, attack_power=4)
    _give(exclusive, "杀伐")
    engine.state.enemies[:] = [exclusive]
    assert engine.combat.check_redemption(exclusive) is None
    exclusive.current_hp = 6
    hit = engine.combat.check_redemption(exclusive)
    assert hit is not None
    r = engine.execute_action("resolve_redemption", {"option": "终结"})
    assert r["success"]
    assert engine.state.friends == []
    assert engine.state.employees == []
    assert not engine.state.pending_redemption
    # 【终结】等同击杀：怪物被还原为一次正常命零，[战终]按命零公式产出碎片。
    assert exclusive.is_alive is False
    assert exclusive.removed_without_kill is False
    assert exclusive.is_departed is False
    assert exclusive.departure_reason == ""


def test_redemption_rejects_bad_accept():
    engine = _ready_combat(_engine("rd_bad"))
    monster = Entity(name="悔怪", entity_type="怪物", blood_limit=40, current_hp=4,
                     attack_count=2, attack_power=2)
    engine.state.enemies[:] = [monster]
    engine.combat.check_redemption(monster)
    missing = engine.execute_action("resolve_redemption", {"option": 1})
    assert not missing["success"]
    assert engine.state.pending_redemption
    dup = engine.execute_action("resolve_redemption", {"option": 1, "name": "贾凡"})
    assert not dup["success"]
    bad = engine.execute_action("resolve_redemption", {"option": 3})
    assert not bad["success"]
    assert engine.state.pending_redemption


def test_redemption_finish_pays_exactly_the_kill_reward():
    """【终结】等同击杀：碎片在[战终]按命零公式结算，与普通击杀同额、不重复计。

    公式（engine/api.py battle_end）：⌈[战始]血限×2%⌉ + 道纹数×5。
    """
    engine = _ready_combat(_engine("rd_finish"))
    monster = Entity(name="油囊", entity_type="怪物", blood_limit=126, current_hp=126,
                     attack_count=2, attack_power=5)
    for name in ("坏死", "衰败", "寄生"):
        _give(monster, name)
    monster.battle_start_blood_limit = 126
    engine.state.enemies[:] = [monster]
    monster.current_hp = 12
    assert engine.combat.check_redemption(monster) is not None

    r = engine.execute_action("resolve_redemption", {"option": 2})
    assert r["success"]
    shards_before = engine.state.shards

    end = engine.execute_action("battle_end", {})
    assert end["success"]
    reward = math.ceil(126 * 0.02) + 3 * 5     # 3 + 15 = 18
    assert end["result"]["shard_reward"] == reward
    assert [d["name"] for d in end["result"]["death_shard_rewards"]] == ["油囊"]
    assert end["result"]["removed_via_alt_path"] == []
    assert engine.state.shards == shards_before + reward


def test_redemption_finish_is_not_double_counted_when_accepted():
    """对照组【接纳】：仍是离场不产碎片（等同击杀只属于【终结】）。"""
    engine = _ready_combat(_engine("rd_accept"))
    monster = Entity(name="壳工", entity_type="怪物", blood_limit=126, current_hp=126,
                     attack_count=2, attack_power=5)
    _give(monster, "坏死")
    monster.battle_start_blood_limit = 126
    engine.state.enemies[:] = [monster]
    monster.current_hp = 12
    assert engine.combat.check_redemption(monster) is not None
    assert engine.execute_action("resolve_redemption", {"option": 1, "name": "壳奴"})["success"]
    end = engine.execute_action("battle_end", {})
    assert end["success"]
    assert end["result"]["shard_reward"] == 0
    assert [d["name"] for d in end["result"]["removed_via_alt_path"]] == ["壳工"]
