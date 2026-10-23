"""
pytest 风格测试 - 里程碑：初拥之夜（9选1特殊事件）

原文（FIRST_EMBRACE_OPTIONS，见 engine/api.py）：
由终音法器"猩红尖牙"强制触发，也可能在其他场景下被再次触发（封存血脉保留触发权）。
1~8每项限选1次，选择后立即回复30%当前[血限]；选项5【真理】为专属道纹，其余项目为遗物；9号"封存血脉"不消耗触发权、可重复触发。

覆盖范围：choose_first_embrace 的门槛校验 + 9个选项各自的具体机制效果。
运行方式：
    python -m pytest tests/test_first_embrace.py -v
"""
import math
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.attack_support import resolve_attack as resolve_player_attack
from engine.api import GameEngine
from engine.consumables import make_wing_item
from engine.daowen import DaoWenEngine
from engine.gamedata import learnability_error, learnable_daowen_pool
from engine.models import Entity, DaoWen, DaoWenInstance, Relic


def _new_engine(region="龙心谷", name="老张", speed=8, mana=6, dbsuffix="a"):
    engine = GameEngine(db_path=f"data/test_embrace_{dbsuffix}.db", rng_seed=1,
                         sealed_candidate_path=f"data/test_embrace_{dbsuffix}_sealed.json")
    blood = 25 - speed - mana
    engine.execute_action("setup_attributes",
                           {"blood_points": blood, "speed_points": speed, "mana_points": mana, "name": name})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    engine.state.energy = 0
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _grant(engine, choice):
    engine.state.pending_first_embrace = True
    return engine.execute_action("choose_first_embrace", {"choice": choice})


def finish_round(engine):
    engine.state.combat_subphase = "await_round_end"
    return engine.execute_action("round_end", {})


def _start_battle_with_enemy(engine, hp=50):
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪物甲", entity_type="怪物", blood_limit=hp, current_hp=hp)
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})
    return enemy


def _resolve_full_attack(engine, attacker_name, target):
    return resolve_player_attack(engine, attacker_name, [0])


# ========================================================================
# 门槛校验
# ========================================================================

def test_choose_without_pending_rejected():
    """错误输入：没有待处理的初拥之夜时拒绝"""
    engine = _new_engine(dbsuffix="gate1")
    r = engine.execute_action("choose_first_embrace", {"choice": 1})
    assert r["success"] is False


def test_choice_out_of_range_rejected():
    """边界：choice必须在1~9之间"""
    engine = _new_engine(dbsuffix="gate2")
    engine.state.pending_first_embrace = True
    assert engine.execute_action("choose_first_embrace", {"choice": 0})["success"] is False
    assert engine.execute_action("choose_first_embrace", {"choice": 10})["success"] is False


def test_truth_is_not_available_through_regular_learning_or_discovery():
    """真理是初拥专属道纹，不进入局外学习或典当发现候选池。"""
    DaoWenEngine.register_all()
    known = DaoWenEngine.list_all()
    error = learnability_error("真理", owned=set(), region="龙心谷", known=known)
    assert error and "初拥之夜专属" in error
    assert "真理" not in learnable_daowen_pool(owned=set(), region="龙心谷", known=known)


def test_truth_cannot_be_embedded_as_a_spell_step():
    """DM中断型真理必须独立发动，避免普通法术数值流程吞掉其裁定效果。"""
    engine = _new_engine(dbsuffix="truth_spell")
    _grant(engine, 5)
    player = engine.state.player
    spell = engine._build_custom_spell(player, {
        "name": "强制真理", "required_daowen": ["真理"],
        "trigger_condition": "受到伤害前", "effect_flow": "发动真理X于攻击者",
    })
    assert "error" in spell and "必须独立发动" in spell["error"]

    enemy = _start_battle_with_enemy(engine)
    with pytest.raises(ValueError, match="必须独立发动"):
        engine.combat.build_instant_execution(
            player, "发动真理X于目标", enemy, [{"x": 1}],
            engine.combat._combat_entity_refs())


def test_cannot_reselect_same_trait_1_to_8():
    """错误输入：1~8号每项限选1次，重复选择被拒绝；9号不受此限制"""
    engine = _new_engine(dbsuffix="gate3")
    r1 = _grant(engine, 1)
    assert r1["success"] is True
    r_dup = _grant(engine, 1)
    assert r_dup["success"] is False
    assert "已经选过" in r_dup["error"]

    r5 = _grant(engine, 5)
    assert r5["success"] is True
    r5_dup = _grant(engine, 5)
    assert r5_dup["success"] is False
    assert "已经选过" in r5_dup["error"]

    r9a = _grant(engine, 9)
    assert r9a["success"] is True
    r9b = _grant(engine, 9)
    assert r9b["success"] is True, "9号可重复选择"


def test_heal_30_percent_blood_limit_on_choice():
    """正常路径：无论选哪一项，都立即回复30%当前血限"""
    engine = _new_engine(dbsuffix="heal")
    player = engine.state.player
    player.current_hp = 1
    r = _grant(engine, 7)  # 血影
    expected = math.ceil(player.blood_limit * 0.3)
    assert r["result"]["healed"] == min(expected, player.blood_limit - 1)


def test_option9_seal_blood_lineage_retains_trigger_right():
    """正常路径：9号封存血脉不获得血脉，也不清空pending_first_embrace（保留再次触发权）"""
    engine = _new_engine(dbsuffix="opt9")
    r = _grant(engine, 9)
    assert r["success"] is True
    assert r["result"]["trait"] == "封存血脉"
    assert engine.state.first_embrace_traits == []
    assert engine.state.pending_first_embrace is True


# ========================================================================
# 1. 血族血脉
# ========================================================================

def test_option1_blood_lineage_heals_when_damage_dealt_else_bleeds_20():
    """正常路径：本回合造成过伤害则[回终]回复等量，否则流血20"""
    engine = _new_engine(dbsuffix="bloodline")
    player = engine.state.player
    _grant(engine, 1)
    enemy = _start_battle_with_enemy(engine)
    player.attack_count = 1
    player.attack_power = 5

    _resolve_full_attack(engine, player.name, enemy)
    player.current_hp = min(player.current_hp, player.blood_limit - 5)
    hp_before_heal = player.current_hp
    r = finish_round(engine)
    heal_effects = [e for e in r["result"]["effects"] if e["type"] == "blood_lineage_heal"]
    assert heal_effects, "本回合造成过伤害应触发回复"
    assert player.current_hp > hp_before_heal

    # 第二回合不出手，应改为流血
    engine.execute_action("round_start", {})
    hp_before_bleed = player.current_hp
    r2 = finish_round(engine)
    bleed_effects = [e for e in r2["result"]["effects"] if e["type"] == "blood_lineage_bleed"]
    assert bleed_effects
    assert player.current_hp == hp_before_bleed - 20


# ========================================================================
# 2. 不朽之躯
# ========================================================================

def test_option2_immortal_body_halves_blood_limit_and_blocks_growth():
    """正常路径：立即血限减半；免疫衰老代价；血限无法增加；属性点修行不受限；获得的法力不超法限"""
    engine = _new_engine(dbsuffix="immortal")
    player = engine.state.player
    base_bl = player.blood_limit
    r = _grant(engine, 2)
    assert r["success"] is True
    assert player.blood_limit == math.ceil(base_bl / 2)

    # 属性点增长与不朽之躯无关：修行照常提升速限（“无法超过上限”只限制获得的当前法力/速度）
    engine.state.energy = 3
    sp_before = player.speed_limit
    # 2属性点=1速限：tier1 只给1点买不到面板，用 tier2（2点）才兑得到 1 速限
    engine.state.shards = max(engine.state.shards, 15)
    r_xiuxing = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 2,
        "allocations": {"speed_points": 2, "mana_points": 0}})
    assert r_xiuxing["success"] is True, r_xiuxing
    assert player.speed_limit == sp_before + 1, "不朽之躯不阻止修行提升速限"
    engine.state.energy = 0

    # 获得的法力无法超过法限：折速战始+24被钳到法限；守夜灯[敌回始]叠加也被钳到法限
    engine.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    engine.state.relics.append(Relic(name="守夜灯", effect="[回始]获得等同于[法限]10%的法力"))
    zhesu = {"折速法印": {"use": True, "x": 4}}
    if any(r.name == "回锋刀" for r in engine.state.relics):
        zhesu["回锋刀"] = {"enemy_index": 0}
    r_bs = engine.execute_action("battle_start", {"relic_choices": zhesu})
    assert r_bs["success"] is True, r_bs
    assert player.current_mana == player.mana_limit, \
        f"不朽之躯获得的法力不得超过法限：当前{player.current_mana} 法限{player.mana_limit}"
    engine.state.enemies.clear()
    engine.execute_action("round_start", {})
    assert player.current_mana == player.mana_limit, \
        f"回始获得法限后仍钳在法限：当前{player.current_mana} 法限{player.mana_limit}"
    engine.combat._grant_shouyedeng(player)
    assert player.current_mana == player.mana_limit, \
        f"守夜灯敌回始叠加的法力也被钳到法限：当前{player.current_mana} 法限{player.mana_limit}"

    # 免疫衰老代价
    engine.state.player.dao_wen["透支"] = DaoWenInstance(
        DaoWen(name="透支", formula="", cost_type="代价", cost_formula="X", effect_formula=""))
    bl_before = player.blood_limit
    r_dw = engine.execute_action("use_daowen", {"daowen_name": "透支", "x": 5})
    assert r_dw["success"] is True
    assert player.blood_limit == bl_before, "不朽之躯应免疫衰老代价"


# ========================================================================
# 3. 血翼（消耗品）
# ========================================================================

def test_option3_grants_wing_consumable_not_relic():
    """正常路径（2026-10-23 重做）：选项3 发放消耗品【血翼】3/3，不占遗物位。"""
    engine = _new_engine(dbsuffix="wing_item")
    engine.state.pending_first_embrace = True
    r = engine.execute_action("choose_first_embrace", {"choice": 3})
    assert r["success"], r.get("error")
    wing = next((c for c in engine.state.consumables if c.name == "血翼"), None)
    assert wing is not None, [c.name for c in engine.state.consumables]
    assert (wing.current_uses, wing.max_uses) == (3, 3)
    assert "血翼" not in [x.name for x in engine.state.relics]
    assert "血翼" not in engine.state.first_embrace_traits


def test_wing_consumable_active_use_grants_flight():
    """正常路径：主动使用获得【飞行】1回合并扣 1 点耐久（消耗品不占出手）。"""
    engine = _new_engine(dbsuffix="wing_use")
    engine.state.consumables.append(make_wing_item("血翼"))
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    player = engine.state.player
    r = engine.execute_action("consume_item", {"name": "血翼"})
    assert r["success"], r
    assert player.get_status_value("飞行") == 1
    assert r["result"]["uses_remaining"] == 2


def test_wing_consumable_boundary_depleted_is_unusable_but_not_destroyed():
    """边界：耐久归零后不可用，但条目仍在（不自动销毁）。"""
    engine = _new_engine(dbsuffix="wing_empty")
    wing = make_wing_item("血翼")
    wing.current_uses = 0
    engine.state.consumables.append(wing)
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    r = engine.execute_action("consume_item", {"name": "血翼"})
    assert r["success"] is False
    assert any(c.name == "血翼" and c.is_depleted for c in engine.state.consumables)


def test_wing_consumable_recharges_every_full_blood_limit_lost():
    """正常路径：每累计失去 100%[血限] 的生命，耐久+1（余数滚存）。"""
    engine = _new_engine(dbsuffix="wing_recharge")
    engine.state.consumables.append(make_wing_item("血翼"))
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    player = engine.state.player
    wing = next(c for c in engine.state.consumables if c.name == "血翼")
    wing.current_uses = 1
    limit = player.blood_limit
    engine.combat.pay_numeric_cost(player, "流血", limit // 2)
    assert wing.current_uses == 1, "未满一个血限不涨耐久"
    engine.combat.pay_numeric_cost(player, "流血", limit - limit // 2)
    assert wing.current_uses == 2, f"满一个血限应+1，实{wing.current_uses}"


def test_wing_consumable_recharge_caps_at_max_durability():
    """边界：耐久已满时不再增长。"""
    engine = _new_engine(dbsuffix="wing_cap")
    engine.state.consumables.append(make_wing_item("血翼"))
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    player = engine.state.player
    wing = next(c for c in engine.state.consumables if c.name == "血翼")
    engine.combat.pay_numeric_cost(player, "流血", player.blood_limit)
    assert wing.current_uses == wing.max_uses == 3


# ========================================================================
# 4. 血族尖牙 + 8. 血食
# ========================================================================

def test_option4_vampire_fang_enslaves_weaker_target_as_chizu():
    """正常路径：衰老20，使生命低于自身的目标转化为赤族"""
    engine = _new_engine(dbsuffix="fang")
    player = engine.state.player
    _grant(engine, 4)
    enemy = _start_battle_with_enemy(engine, hp=5)
    bl_before = player.blood_limit

    r = engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    assert r["success"] is True
    assert player.blood_limit == bl_before - 20
    assert "怪物甲" in engine.state.chizu_names
    chizu = next(f for f in engine.state.friends if f.name == "怪物甲")
    assert chizu.entity_type == "赤族"
    assert chizu.is_chizu_of == player.name
    assert enemy not in engine.state.enemies


def test_option4_error_target_not_weaker_or_missing():
    """错误输入：目标生命不低于自身，或目标不存在/已死亡"""
    engine = _new_engine(dbsuffix="fang_err")
    _grant(engine, 4)
    _start_battle_with_enemy(engine, hp=99999)
    r = engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    assert r["success"] is False

    r2 = engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:99"})
    assert r2["success"] is False


def test_option8_blood_feast_kills_chizu_and_heals_equal_amount():
    """正常路径：命零一名赤族，自身获得等同其当前生命的回复"""
    engine = _new_engine(dbsuffix="feast")
    player = engine.state.player
    _grant(engine, 4)
    _grant(engine, 8)
    _start_battle_with_enemy(engine, hp=5)
    engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    chizu = next(f for f in engine.state.friends if f.name == "怪物甲")
    chizu.current_hp = 18
    player.current_hp = 10
    r = engine.execute_action("blood_feast", {"target_ref": "friend:0"})
    assert r["success"] is True
    assert chizu.is_alive is False
    assert player.current_hp == 10 + 18


def test_option8_error_without_trait_or_invalid_chizu():
    """错误输入：没有血食血脉时拒绝；指定的赤族不存在时拒绝"""
    engine = _new_engine(dbsuffix="feast_err")
    r = engine.execute_action("blood_feast", {"target_ref": "friend:99"})
    assert r["success"] is False
    _grant(engine, 8)
    r2 = engine.execute_action("blood_feast", {"target_ref": "friend:99"})
    assert r2["success"] is False


def test_chizu_curse_bleeds_20_each_round_end():
    """正常路径：赤族诅咒——存活的赤族[回终]固定流血20"""
    engine = _new_engine(dbsuffix="curse")
    _grant(engine, 4)
    _start_battle_with_enemy(engine, hp=5)
    engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    chizu = next(f for f in engine.state.friends if f.name == "怪物甲")
    chizu.current_hp = 30
    finish_round(engine)
    assert chizu.current_hp == 10


# ========================================================================
# 5. 真理
# ========================================================================

def test_option5_truth_is_a_daowen_and_raises_dm_interrupt_without_spending_action():
    """正常路径：选项5授予专属道纹；效果仍交DM裁定且不消耗出手。"""
    engine = _new_engine(dbsuffix="truth")
    player = engine.state.player
    result = _grant(engine, 5)
    assert result["success"] is True
    assert result["result"]["trait"] == "真理"
    assert "真理" in player.dao_wen
    assert "真理" not in [relic.name for relic in engine.state.relics]
    assert "真理眼" not in [relic.name for relic in engine.state.relics]
    assert "真理" not in engine.state.first_embrace_traits

    enemy = _start_battle_with_enemy(engine)
    available = engine.get_available_actions()["actions"]
    truth_action = next(action for action in available
                        if action.get("action_type") == "use_daowen"
                        and action.get("params_schema", {}).get("daowen_name") == "真理")
    assert "target_ref" in truth_action["params_schema"]
    assert "statement" in truth_action["params_schema"]
    # 2026-10-23 用户裁定：X = 陈述句数，不再是固定1
    assert "x" in truth_action["params_schema"]
    assert not any(action.get("action_type") == "use_truth_eye" for action in available)

    actions_before = player.actions_used_this_round
    hp_before, mana_before = player.current_hp, player.current_mana
    r = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "target_ref": "enemy:0", "statement": "你藏着钥匙"})
    assert r["success"] is True
    assert r["action"] == "真理X=1"
    assert r["result"]["interrupt"]["context"]["ability"] == "真理"
    assert "请DM逐句裁定是否属实" in r["result"]["interrupt"]["description"]
    assert player.dao_wen["真理"].cooldown_remaining == 2
    assert player.actions_used_this_round == actions_before
    assert (player.current_hp, player.current_mana) == (hp_before, mana_before)
    assert len(engine._pending_interrupts) == 1


def test_legacy_truth_eye_action_alias_routes_to_the_new_daowen():
    """兼容旧客户端的 use_truth_eye action 名称，但统一按新道纹结算。"""
    engine = _new_engine(dbsuffix="truth_alias")
    _grant(engine, 5)
    _start_battle_with_enemy(engine)
    result = engine.execute_action("use_truth_eye", {
        "target_ref": "enemy:0", "statement": "旧客户端兼容"})
    assert result["success"] is True
    assert result["action"] == "真理X=1"
    assert result["result"]["interrupt"]["context"]["ability"] == "真理"
    assert engine.state.player.dao_wen["真理"].cooldown_remaining == 2


def test_option5_cooldown_blocks_reuse_and_decrements_per_battle_end():
    """边界：冷却期内再次发动应拒绝；冷却随每次[战终]递减，归零后可再次使用"""
    engine = _new_engine(dbsuffix="truth_cd")
    _grant(engine, 5)
    _start_battle_with_enemy(engine)
    engine.execute_action("use_daowen", {
        "daowen_name": "真理", "target_ref": "enemy:0", "statement": "第一次"})
    engine.submit_ruling(interrupt_type="自定义", ruling_text="属实")

    r_again = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "target_ref": "enemy:0", "statement": "立刻再来"})
    assert r_again["success"] is False, "冷却期内应拒绝"

    engine.state.enemies.clear()
    engine.execute_action("battle_end", {})
    assert engine.state.player.dao_wen["真理"].cooldown_remaining == 1
    engine.state.phase = "in_combat"  # 构造下一场已结束的合法战终状态
    engine.execute_action("battle_end", {})
    assert engine.state.player.dao_wen["真理"].cooldown_remaining == 0

    engine.state.phase = "in_combat"
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="怪物乙", entity_type="怪物", blood_limit=10, current_hp=10))
    r_ok = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "target_ref": "enemy:0", "statement": "冷却结束"})
    assert r_ok["success"] is True


def test_truth_cooldown_round_trips_through_game_save(tmp_path):
    """存档往返后，真理的战斗场数冷却保存在专属道纹实例上。"""
    engine = _new_engine(dbsuffix="truth_save")
    engine.save_dir = str(tmp_path)
    _grant(engine, 5)
    _start_battle_with_enemy(engine)
    used = engine.execute_action("use_daowen", {
        "daowen_name": "真理", "target_ref": "enemy:0", "statement": "存档冷却"})
    assert used["success"] is True
    assert engine.state.player.to_dict()["daowen_cooldowns"]["真理"]["battles"] == 2
    assert engine.save_game("truth")["success"] is True

    restored = GameEngine(db_path=str(tmp_path / "restored.db"), save_dir=str(tmp_path),
                          rng_seed=1, sealed_candidate_path=str(tmp_path / "sealed.json"))
    loaded = restored.load_game("truth")
    assert loaded["success"] is True
    assert restored.state.player.dao_wen["真理"].cooldown_remaining == 2
    assert len(restored._pending_interrupts) == 1


def test_legacy_truth_eye_save_migrates_relic_and_cooldown(tmp_path):
    """旧存档里的真理眼遗物与全局冷却迁为真理道纹实例。"""
    engine = _new_engine(dbsuffix="truth_legacy")
    engine.save_dir = str(tmp_path)
    engine.state.grant_relic("真理眼", "旧版效果", tag="血族")
    engine.state.truth_eye_cooldown = 1
    assert engine.save_game("legacy_truth")["success"] is True

    restored = GameEngine(db_path=str(tmp_path / "legacy_restored.db"), save_dir=str(tmp_path),
                          rng_seed=1, sealed_candidate_path=str(tmp_path / "sealed_legacy.json"))
    loaded = restored.load_game("legacy_truth")
    assert loaded["success"] is True
    assert "真理" in restored.state.player.dao_wen
    assert restored.state.player.dao_wen["真理"].cooldown_remaining == 1
    assert all(relic.name != "真理眼" for relic in restored.state.relics)
    assert restored.state.truth_eye_cooldown == 0
    assert "真理" not in restored.state.first_embrace_traits


# ========================================================================
# 6. 寒冰法力
# ========================================================================

def test_option6_frost_mana_reduces_target_action_count_per_10_points():
    """正常路径：持有者对任意目标(含自身)累计施加满10点消耗法力，使其本回合出手次数-1"""
    # 杀伐 x=10 要付 10 法力 → 法限须 ≥10（mana_points=20 → 法限10，速4 血1，共25）
    engine = _new_engine(dbsuffix="ice", speed=4, mana=20)
    player = engine.state.player
    _grant(engine, 6)
    enemy = _start_battle_with_enemy(engine, hp=1000)
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 10, "target": "怪物甲"})
    assert r["success"] is True
    assert enemy.mana_inflicted_this_round == 10
    assert enemy.get_status_value("无力") == 1


def test_option6_boundary_below_10_does_not_stack():
    """边界：不足10点时不叠加【无力】，跨越多个10的倍数一次性叠加对应层数"""
    # 提高法限分配，使单次施放能一次性跨越多个10点门槛
    # 法点必须偶数（向下取偶后 21 非法）：20 点 → 法限10
    engine = _new_engine(dbsuffix="ice_bound", speed=2, mana=20)
    player = engine.state.player
    player.speed_limit = player.current_speed = 6  # 同回合允许两次施放
    # 本条要在同回合花掉 5+25=30 法力；25 点开局预算最多给到法限12，
    # 直接抬法限（本条测的是【无力】按累计法力叠层，与法限定价无关）。
    player.mana_limit = player.current_mana = 40
    _grant(engine, 6)
    enemy = _start_battle_with_enemy(engine, hp=1000)
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target": "怪物甲"})
    assert enemy.get_status_value("无力") == 0, "未满10点不应叠加"
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 25, "target": "怪物甲"})
    # 累计5+25=30，跨越3个10 => 3层
    assert enemy.mana_inflicted_this_round == 30
    assert enemy.get_status_value("无力") == 3


# ========================================================================
# 7. 血影
# ========================================================================

def test_option7_blood_shadow_negates_non_must_hit_attack_by_bleeding_10():
    """正常路径：非必中判定下，可流血10取消本次判定（不同于常规闪避）"""
    engine = _new_engine(dbsuffix="shadow")
    player = engine.state.player
    _grant(engine, 7)
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    attacker = Entity(name="攻击者", entity_type="怪物", blood_limit=30, current_hp=30, attack_power=50)
    engine.state.enemies.append(attacker)
    engine.execute_action("round_start", {})
    player.current_hp = 60
    hp_before = player.current_hp
    result = engine.combat.resolve_attack(attacker, player, is_must_hit=False, blood_shadow=True)
    assert result.get("blood_shadow_success") is True
    assert player.current_hp == hp_before - 10
    assert result["damage_dealt"] == 0


def test_option7_boundary_must_hit_cannot_be_negated_by_blood_shadow():
    """边界：必中判定无法用血影取消"""
    engine = _new_engine(dbsuffix="shadow_must")
    player = engine.state.player
    _grant(engine, 7)
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    attacker = Entity(name="攻击者", entity_type="怪物", blood_limit=30, current_hp=30, attack_power=10)
    engine.state.enemies.append(attacker)
    engine.execute_action("round_start", {})
    player.current_hp = 60
    result = engine.combat.resolve_attack(attacker, player, is_must_hit=True, blood_shadow=True)
    assert "blood_shadow_success" not in result
    assert result["damage_dealt"] == 10
