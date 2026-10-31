"""
pytest 风格测试 - 里程碑：三副本终音法器

原文（TERMINAL_ARTIFACTS，见 engine/api.py）：
死斗胜利后，按 current_region 从对应副本的4选1（扭曲都市/罪孽都市）或3选1（龙心谷）
终音法器池中领取一件，随后才完整封存（猩红尖牙例外：需先完成初拥之夜）。

覆盖范围：
1. choose_terminal_artifact 的门槛校验（无待领取/choice越界）。
2. 扭曲都市4件：体外心脏/羔羊之泪/红头绳(献祭)/猩红尖牙(触发初拥之夜后才封存)。
3. 罪孽都市3件：黑金名片(含"负债校验必须在状态突变前完成"的回归测试)/罪业金库/教父左轮。
4. 龙心谷3件：共心环(法力共享门槛)/负岳碑(预声明保护朋友/员工撤退)。真龙之心的资源/遗物
   本身在 tests/test_dragon_traits.py 中覆盖，这里只测试"领取该法器"这一步。

运行方式：
    python -m pytest tests/test_terminal_artifacts.py -v
"""
import os
import sys
from pathlib import Path

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, Consumable


def _cleanup(path):
    if os.path.exists(path):
        os.remove(path)


def _new_engine(region, name="老张", speed=8, mana=6, dbsuffix="a", sealed="data/test_artifact_sealed.json"):
    engine = GameEngine(db_path=f"data/test_artifact_{dbsuffix}.db", rng_seed=1, sealed_candidate_path=sealed)
    blood = 25 - speed - mana
    engine.execute_action("setup_attributes",
                           {"blood_points": blood, "speed_points": speed, "mana_points": mana, "name": name})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _start_battle(engine, *, start_round=True):
    engine.state.energy = 0
    result = engine.execute_action("battle_start", {"relic_choices": {}})
    if result.get("success") and start_round:
        engine.execute_action("round_start", {"relic_choices": {}})
    return result


# ========================================================================
# choose_terminal_artifact 门槛校验
# ========================================================================

def test_choose_terminal_artifact_without_pending_is_rejected():
    """错误输入：没有待领取的终音法器时直接拒绝"""
    engine = _new_engine("龙心谷", dbsuffix="gate1")
    r = engine.execute_action("choose_terminal_artifact", {"choice": 1})
    assert r["success"] is False
    assert "没有待领取" in r["error"]


def test_choose_terminal_artifact_out_of_range_choice_rejected():
    """边界：choice超出该副本法器数量范围（龙心谷共3件）时拒绝，且不消耗pending状态"""
    engine = _new_engine("龙心谷", dbsuffix="gate2")
    engine.state.pending_terminal_region = "龙心谷"
    r_high = engine.execute_action("choose_terminal_artifact", {"choice": 99})
    assert r_high["success"] is False
    r_zero = engine.execute_action("choose_terminal_artifact", {"choice": 0})
    assert r_zero["success"] is False
    assert engine.state.pending_terminal_region == "龙心谷", "校验失败不应清空pending状态"


# ========================================================================
# 扭曲都市：体外心脏 / 羔羊之泪 / 红头绳 / 猩红尖牙
# ========================================================================

def test_body_outside_heart_doubles_and_reverts():
    """正常路径：体外心脏[战始]血限与当前生命翻倍，[战终]恢复原值"""
    engine = _new_engine("扭曲都市", dbsuffix="heart")
    player = engine.state.player
    engine.state.artifacts_owned.append("体外心脏")
    base_bl, base_hp = player.blood_limit, player.current_hp
    _start_battle(engine)
    assert player.blood_limit == base_bl * 2
    assert player.current_hp == base_hp * 2
    for enemy in engine.state.enemies:
        enemy.current_hp = 0
        enemy.is_alive = False
    engine.state.monster_reinforcements.clear()   # 夹具只打这一波
    ended = engine.execute_action("battle_end", {})
    assert ended["success"], ended
    assert player.blood_limit == base_bl
    assert player.current_hp == base_hp


def test_lamb_tears_halves_everyone_on_battle_start():
    """正常路径：羔羊之泪[战始]场上所有角色与怪物立刻失去50%当前生命"""
    engine = _new_engine("扭曲都市", dbsuffix="lamb")
    engine.state.artifacts_owned.append("羔羊之泪")
    player = engine.state.player
    player.current_hp = 100
    player.blood_limit = 100
    _start_battle(engine)
    assert player.current_hp == 50
    assert engine.state.enemies, "本回合应有抽取到的怪物"
    for m in engine.state.enemies:
        assert m.current_hp <= m.blood_limit


def test_red_string_unlocks_sacrifice_action():
    """正常路径：红头绳解锁局外【献祭】，衰老3换精力+2；未持有时拒绝"""
    engine = _new_engine("扭曲都市", dbsuffix="redstring")
    player = engine.state.player

    r_fail = engine.execute_action("pre_battle_action", {"sub_action": "献祭"})
    assert r_fail["success"] is False

    engine.state.artifacts_owned.append("红头绳")
    engine.state.has_sacrifice_action = True
    before_bl = player.blood_limit
    before_energy = engine.state.energy
    r_ok = engine.execute_action("pre_battle_action", {"sub_action": "献祭"})
    assert r_ok["success"] is True
    assert player.blood_limit == before_bl - 3
    assert engine.state.energy == before_energy - 1 + 2


def test_crimson_fang_triggers_first_embrace_then_seals():
    """正常路径：猩红尖牙领取后强制触发初拥之夜；完成选择前不封存，完成后（不论选几号）才封存"""
    sealed = "data/test_artifact_fang_sealed.json"
    _cleanup(sealed)
    engine = _new_engine("扭曲都市", name="种子候选", dbsuffix="fang_seed", sealed=sealed)
    engine.state.current_battle = 7
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.phase = "in_combat"
    engine.execute_action("battle_end", {})
    # 第7场战终不再自动封存（改为【最终的冠冕】世界事件）；本文件测终音法器，
    # 显式走旧的封存入口。
    engine._trigger_final_crown()  # 无候选，直接封存

    challenger = _new_engine("扭曲都市", name="挑战者", speed=12, dbsuffix="fang_challenger", sealed=sealed)  # 速点必须偶数
    challenger.state.current_battle = 7
    challenger.state.enemies.clear()
    challenger.state.monster_reinforcements.clear()
    challenger.state.phase = "in_combat"
    challenger.execute_action("battle_end", {})
    challenger._trigger_final_crown()
    assert challenger.state.in_final_duel
    challenger.execute_action("resolve_final_duel", {"outcome": "victory"})

    r = challenger.execute_action("choose_terminal_artifact", {"choice": 4})  # 猩红尖牙
    assert r["success"] is True
    assert r["result"]["first_embrace_pending"] is True
    assert challenger.state.pending_first_embrace is True
    assert not os.path.exists(sealed), "初拥之夜完成前不应封存"

    r2 = challenger.execute_action("choose_first_embrace", {"choice": 9})  # 封存血脉，保留触发权
    assert r2["success"] is True
    assert os.path.exists(sealed), "即便选9号，只要是猩红尖牙触发的这次，完成选择后也应封存"

    import json
    with open(sealed, encoding="utf-8") as f:
        snapshot = json.load(f)["candidates"]["2"][0]  # 一阶死斗胜者进入二阶进阶封存
    assert snapshot["pending_first_embrace"] is True, "封存快照应保留9号的再次触发权"
    assert "猩红尖牙" in snapshot["artifacts_owned"]
    _cleanup(sealed)


# ========================================================================
# 罪孽都市：黑金名片 / 罪业金库 / 教父左轮
# ========================================================================

def test_black_gold_card_debt_limit_rejects_before_mutating_state():
    """错误输入 + 回归测试：负债超过50时应直接拒绝，且不能提前修改敌方血限（曾经的真实bug）"""
    engine = _new_engine("罪孽都市", dbsuffix="card_debt")
    engine.state.artifacts_owned.append("黑金名片")
    engine.state.shards = 10
    _start_battle(engine, start_round=False)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    big = Entity(name="大怪", entity_type="怪物", blood_limit=200, current_hp=200)
    engine.state.enemies.append(big)

    r = engine.execute_action("use_black_card", {})
    assert r["success"] is False
    assert big.blood_limit == 200, "校验失败时不应已经把敌方血限减半"
    assert engine.state.shards == 10, "校验失败时不应扣碎片"


def test_black_gold_card_success_path():
    """正常路径：碎片充足时，敌方血限减半，扣除等量碎片"""
    engine = _new_engine("罪孽都市", dbsuffix="card_ok")
    engine.state.artifacts_owned.append("黑金名片")
    engine.state.shards = 200
    _start_battle(engine, start_round=False)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    big = Entity(name="大怪", entity_type="怪物", blood_limit=200, current_hp=200)
    engine.state.enemies.append(big)
    r = engine.execute_action("use_black_card", {})
    assert r["success"] is True
    assert big.blood_limit == 100
    assert engine.state.shards == 100


def test_crime_vault_boundary_2_percent_cap():
    """边界：X最多为当前碎片的2%，超出应拒绝，边界值本身应成功"""
    engine = _new_engine("罪孽都市", dbsuffix="vault")
    engine.state.artifacts_owned.append("罪业金库")
    engine.state.shards = 1000
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "await_round_start"
    player = engine.state.player
    r_ok = engine.execute_action("use_crime_vault", {"x": 20})  # 2%×1000=20
    assert r_ok["success"] is True
    assert player.shield == 40
    assert engine.state.shards == 980

    r_over = engine.execute_action("use_crime_vault", {"x": 20})  # 现碎片980，2%=19，20越界
    assert r_over["success"] is False


def test_godfather_revolver_escalates_and_refills_next_battle():
    """正常路径：教父左轮伤害随本场使用次数递增；弹药耗尽后拒绝；下一场[战始]回满"""
    engine = _new_engine("罪孽都市", dbsuffix="gun")
    engine.state.artifacts_owned.append("教父左轮")  # 纯法器：弹药由本场使用次数单点计数
    player = engine.state.player
    _start_battle(engine)
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    foe = Entity(name="靶子", entity_type="怪物", blood_limit=100000, current_hp=100000)
    engine.state.enemies.append(foe)

    import math
    base = math.ceil(player.blood_limit * 0.3)
    for i in range(1, 7):
        r = engine.execute_action("fire_godfather_revolver", {"target_ref": "enemy:0"})
        assert r["success"] is True
        assert r["result"]["damage"] == base * i

    r_out = engine.execute_action("fire_godfather_revolver", {"target_ref": "enemy:0"})
    assert r_out["success"] is False, "6发耗尽后应拒绝"

    assert "打空" in r_out.get("error", ""), r_out

    foe.current_hp = 0
    foe.is_alive = False
    engine.execute_action("battle_end", {})
    _start_battle(engine)
    assert engine.state.godfather_revolver_uses == 0, "下一场[战始]弹药回满"
    assert not [c for c in engine.state.consumables if c.name == "教父左轮"], \
        "教父左轮是法器，不该再以消耗品身份留在背包里"
    assert engine.state.enemies, "新一场应有敌人"
    engine.state.enemies[0].current_hp = 100000
    engine.state.enemies[0].blood_limit = 100000
    r_refill = engine.execute_action("fire_godfather_revolver", {"target_ref": "enemy:0"})
    assert r_refill["success"] is True, r_refill
    assert r_refill["result"]["ammo_remaining"] == 5, r_refill


def test_godfather_revolver_is_artifact_only():
    """静态守护：教父左轮只是法器，不得再以消耗品身份重复记账。

    2026-10-26 用户令：早期消耗品没有自动回满耐久的机制，才借了「消耗品+法器」
    双重身份；现在回满就是[战始]重置，弹药由 `godfather_revolver_uses` 单点计数。
    """
    src = (Path(__file__).resolve().parents[1] / "engine" / "api.py").read_text(encoding="utf-8")
    assert 'kind="artifact_weapon"' not in src, "教父左轮不该再建 artifact_weapon 消耗品"
    assert 'name="教父左轮", effect=effect' not in src
    assert "GODFATHER_REVOLVER_AMMO" in src, "弹药数应有单点常量"


# ========================================================================
# 龙心谷：共心环 / 负岳碑
# ========================================================================

def test_shared_dragon_heart_ring_requires_owned_heart_of_that_type():
    """错误输入：选择一个自己并未持有的龙心类型应拒绝"""
    engine = _new_engine("龙心谷", dbsuffix="ring_err")
    engine.state.artifacts_owned.append("共心环")
    engine.state.phase = "in_combat"; engine.state.combat_subphase = "await_round_start"
    r = engine.execute_action("select_shared_dragon_heart", {"dragon_heart_type": "衰老"})
    assert r["success"] is False
    engine.state.consumables.append(Consumable(
        name="衰老龙心", effect="", current_uses=3, max_uses=3,
        kind="dragon_heart", dragon_heart_type="衰老"))
    ok = engine.execute_action("select_shared_dragon_heart", {"dragon_heart_type": "衰老"})
    assert ok["success"] and engine.state.shared_dragon_heart_type == "衰老"


def test_fuyuebei_toll_protects_ally_from_retreat_and_costs_player_20():
    """正常路径：预声明保护后，友方即将撤退时改为玩家流血20，取消本次伤害与撤退"""
    engine = _new_engine("龙心谷", dbsuffix="toll")
    player = engine.state.player
    engine.state.artifacts_owned.append("负岳碑")
    ally = Entity(name="队友甲", entity_type="朋友", blood_limit=30, current_hp=10, is_deployed=True)
    engine.state.friends.append(ally)
    engine.state.phase = "in_combat"
    engine.execute_action("declare_fuyuebei_toll", {"target_ref": "friend:0"})
    player.current_hp = 40

    result = engine.combat._apply_hostile_damage(ally, 9999, "普通")
    assert result.get("fuyuebei_toll_paid") == 20
    assert ally.current_hp == 10, "被保护的友方不应掉血"
    assert ally.has_retreated is False
    assert player.current_hp == 20, "玩家应流血20"
    assert "队友甲" not in engine.state.fuyuebei_declared, "用后即耗尽，需重新声明"


def test_fuyuebei_toll_boundary_player_hp_too_low_falls_back_to_retreat():
    """边界：玩家当前生命≤20时不满足触发条件，退回常规撤退逻辑"""
    engine = _new_engine("龙心谷", dbsuffix="toll_low")
    player = engine.state.player
    engine.state.artifacts_owned.append("负岳碑")
    ally = Entity(name="队友乙", entity_type="朋友", blood_limit=30, current_hp=10, is_deployed=True)
    engine.state.friends.append(ally)
    engine.execute_action("declare_fuyuebei_toll", {"target_ref": "friend:0"})
    player.current_hp = 15

    result = engine.combat._apply_hostile_damage(ally, 9999, "普通")
    assert "fuyuebei_toll_paid" not in result
    assert ally.has_retreated is True
