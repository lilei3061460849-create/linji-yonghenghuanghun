"""必中：层数制、攻击与道纹**共用**层数（报告.md 硬伤2-A/B）。

DM 口径（`规则正文.md` 杀伐闭环）：必中X = 自身下X次**选择[目标]**时其无法闪避。
「选择[目标]」包含攻击判定与道纹判定，两者共用同一份层数理，不浪费、不重复计数。

本文件把三条结算路径钉在一起：
  - 攻击路径：combat.resolve_attack（怪物/微光者）与 api._action_resolve_attack（玩家）
  - 玩家道纹路径：api._resolve_daowen_dodge
  - 怪物道纹路径：combat._resolve_monster_daowen_choice（含 **debuff 类道纹**）

硬伤2-B 的实测结论：怪物 debuff 类道纹（勾魂/镇尸/减速/封印/衰弱）**同样**走
闪避判定；怪物持必中层时消耗 1 层即可让该 debuff 无法被抵抗，否则玩家照常闪避。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect  # noqa: E402
from tests.monster_phase_support import resolve_monster_phase  # noqa: E402
from tests.setup_support import (finish_initial_daowen, grant_extra_actions,
                                 opponent_passes)  # noqa: E402


def _engine(suffix, region="乱葬岗"):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=f"/tmp/linji_tests/test_bizhong_{suffix}.db", rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_bizhong_s_{suffix}.json")
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": region})
    e.state.phase = "in_combat"
    e.state.current_round = 2          # 跳过白板回合
    return e


def _monster(daowen, name="寄骨蝇", hp=200, atk=20, hits=1):
    # 2026-09-16：怪物[法限]即法力池，atk 同时就是它的法力上限。原默认值 atk=1
    # 付不起本文件用到的道纹（勾魂X/衰弱X 等），抬到 20。本文件只断言必中/闪避
    # 与目标选择，不断言怪物伤害。
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=hits, attack_power=atk)
    for dw, x in daowen.items():
        m.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=x)
    return m


def _monster_cast(e, name, dodge):
    """怪物阶段：指定道纹指向 player:0，玩家提交 dodge。返回 (结果, 剩余层)。"""
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    opt = next(o for o in actor["daowen_options"] if o["name"] == name)
    dao = {"name": name, "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {}}
    if opt["requires_target"]:
        dao["target_ref"] = "player:0"
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False,
                          "blood_shadow": False,
                          "spell_choices": {"before": {}, "after": {}}}
                         for _ in range(actor["base_hits_per_attack"])]}
               for _ in range(actor["base_attack_actions"])]
    # 交替出手：一次提交＝一个行动，发动道纹时不再捆绑攻击组。
    res = e.combat.resolve_monster_phase(
        [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": []}],
        prepared=prepared)
    return res


# ==================== B. 怪物 debuff 道纹 与 必中 ====================

def test_debuff_daowen_is_dodgeable_without_bizhong():
    """对照：怪物无必中层时，debuff 类道纹（勾魂）可被玩家闪避。"""
    e = _engine("debuff_nobz")
    p = e.state.player
    p.current_speed = 6
    m = _monster({"勾魂": 4})
    e.state.enemies.append(m)
    # 玩家提交 dodge 由 resolve 侧决定：这里直接走引擎的 dodge 分支
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    opt = next(o for o in actor["daowen_options"] if o["name"] == "勾魂")
    assert opt["requires_target"], "勾魂必须要求显式目标"
    dao = {"name": "勾魂", "target_ref": "player:0", "dodge": True,
           "blood_shadow": False, "trigger_spell_choices": {}}
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False,
                          "blood_shadow": False,
                          "spell_choices": {"before": {}, "after": {}}}]}]
    res = e.combat.resolve_monster_phase(
        [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": []}],
        prepared=prepared)
    entry = next(d for d in res if "daowen_activated" in d)
    assert entry.get("dodged") is True, entry
    assert not p.has_status("勾魂"), "闪避成功后不得挂上勾魂"


def test_bizhong_makes_debuff_unresistable_and_consumes_layer():
    """硬伤2-B：怪物持必中层 → debuff 无法被抵抗，消耗 1 层。"""
    e = _engine("debuff_bz")
    p = e.state.player
    p.current_speed = 6
    m = _monster({"勾魂": 4})
    e.state.enemies.append(m)
    e.combat.grant_bizhong(m, 2)
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    dao = {"name": "勾魂", "target_ref": "player:0", "dodge": True,
           "blood_shadow": False, "trigger_spell_choices": {}}
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False,
                          "blood_shadow": False,
                          "spell_choices": {"before": {}, "after": {}}}]}]
    res = e.combat.resolve_monster_phase(
        [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": []}],
        prepared=prepared)
    entry = next(d for d in res if "daowen_activated" in d)
    assert not entry.get("dodged"), entry
    assert p.has_status("勾魂"), "必中压下 debuff 必须生效"
    # 一次提交只有道纹一个行动：道纹判定吃 1 层（2 → 1）。
    # 「攻击判定也吃同一份层数」由 test_attack_and_daowen_share_one_pool 覆盖。
    assert e.combat.bizhong_remaining(m) == 1, e.combat.bizhong_remaining(m)


# ==================== A. 攻击与道纹共用层数 ====================

def test_player_daowen_consumes_bizhong_layer():
    """玩家敌向道纹：消耗 1 层，目标无法闪避。"""
    e = _engine("player_dw")
    p = e.state.player
    p.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    m = _monster({}, name="靶怪")
    m.current_speed = 5
    e.state.enemies.append(m)
    e.execute_action("round_start", {})
    e.combat.grant_bizhong(p, 2)
    hp0 = m.current_hp
    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3,
                                        "target_ref": "enemy:0", "dodge": True,
                                        "blood_shadow": False,
                                        "trigger_spell_choices": {}})
    assert r.get("success") is True, r.get("error")
    assert e.combat.bizhong_remaining(p) == 1, "道纹判定应消耗 1 层"
    assert m.current_hp == hp0 - 9, "必中压下目标无法闪避，伤害照常结算（杀伐3→X²=9）"


def test_attack_and_daowen_share_one_pool():
    """A 的核心：攻击与道纹从**同一份**层数里扣，不各算一套。"""
    e = _engine("shared")
    p = e.state.player
    p.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    m = _monster({}, name="靶怪")
    m.current_speed = 5
    e.state.enemies.append(m)
    # 轮回者默认没有普攻（attack_count=0），这里给一次普攻以覆盖攻击路径
    p.attack_count, p.attack_power = 1, 3
    e.execute_action("round_start", {})
    e.combat.grant_bizhong(p, 2)
    assert e.combat.bizhong_remaining(p) == 2
    # ① 道纹判定扣 1
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1,
                                    "target_ref": "enemy:0", "dodge": True,
                                    "blood_shadow": False,
                                    "trigger_spell_choices": {}})
    assert e.combat.bizhong_remaining(p) == 1
    # ② 普攻判定从同一份里再扣 1
    # 交替出手：两次主动行动之间必须经过敌方【跳过】；并给轮回者第二次出手额度。
    grant_extra_actions(p, 1)
    opponent_passes(e)
    p.current_speed = 1   # 攻次=当前速度：压到1，本条只提交1个hit
    prep = e.execute_action("prepare_attack", {})
    assert prep.get("success") is True, prep.get("error")
    tok = prep["result"]["token"]
    opt = prep["result"]["target_options"][0]
    res = e.execute_action("resolve_attack", {"token": tok, "hits": [
        {"target_ref": opt["ref"], "dodge": True, "blood_shadow": False,
         "spell_choices": {"before": {}, "after": {}}}]})
    assert res.get("success") is True, res.get("error")
    hit = res["result"]["hits"][0]
    assert hit["dodge_attempted"] is True and hit["dodge_success"] is False, hit
    assert hit["dodge_fail_reason"] == "必中攻击无法闪避", hit
    assert m.current_speed == 5, "必中压下目标不必付速度闪避"
    assert e.combat.bizhong_remaining(p) == 0, "攻击与道纹必须共用层数"


def test_bizhong_layers_persist_across_rounds_until_used():
    """边界：层数不因[回终]/[回始]清零，只会因"选择[目标]"被消耗。"""
    e = _engine("persist")
    p = e.state.player
    m = _monster({}, name="靶怪")
    e.state.enemies.append(m)
    e.combat.grant_bizhong(p, 3)
    e.execute_action("round_start", {})
    assert e.combat.bizhong_remaining(p) == 3
    assert p.has_status("必中")


# ==================== 硬伤2 整环：必中 → debuff → 逼玩家用残韵 ====================

def test_monster_cast_bizhong_self_then_forces_debuff():
    """整环（2026-09-28 必中X最终=self-buff口径）：怪物施放必中X=3——给**自身**挂【必中】
    姿态持续3回合；下一手怪物放勾魂，玩家提交 dodge 也无法闪避/抵抗（必中姿态下
    怪物选中的目标无法闪避），被挂上勾魂。
    """
    e = _engine("loop")
    p = e.state.player
    p.current_speed = 6
    # 本用例只验证必中姿态、勾魂与残韵出路；避免无关普攻把玩家打入死者之页中断。
    p.blood_limit = p.current_hp = 1000
    m = _monster({"必中": 3, "勾魂": 4}, name="勾魂使者")
    e.state.enemies.append(m)
    e.combat.reset_monster_activation()

    # 怪物对自己施必中3（self-buff，无目标）→ 怪物身上挂【必中】姿态
    out1 = resolve_monster_phase(e.combat, {"enemy:0": "必中"})
    assert any(isinstance(d, dict) and d.get("daowen_activated") == "必中"
               for d in out1), out1
    assert m.has_status("必中"), "怪物施必中X后自身应进入【必中】姿态"

    # 下一回合：怪物放勾魂 → 怪物处于必中姿态 → 玩家无法闪避/抵抗 → 勾魂生效
    e.state.current_round = 3
    m.actions_used_this_round = 0   # 手工推进回合：同步重置通用出手预算
    e.combat.reset_monster_activation()
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    dao = {"name": "勾魂", "target_ref": "player:0", "dodge": True,
           "blood_shadow": False, "trigger_spell_choices": {}}
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False,
                          "blood_shadow": False,
                          "spell_choices": {"before": {}, "after": {}}}
                         for _ in range(actor["base_hits_per_attack"])]}
               for _ in range(actor["base_attack_actions"])]
    e.combat.resolve_monster_phase(
        [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": []}],
        prepared=prepared)
    assert p.has_status("勾魂"), "怪物处于必中姿态，勾魂必须命中生效"

    # 【冥气】已遗物化删除；勾魂的【反转】路径现在指向不可获得的「？？」占位。
    # 尝试残韵不得消耗资源，也不能凭空获得已删除道纹。
    e.state.resonance = {"反转": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "勾魂",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is False, r.get("error")
    assert "？？" in r.get("error", "")
    assert "勾魂" in m.dao_wen and "冥气" not in m.dao_wen
    assert e.state.resonance.get("反转", 0) == 1
    assert p.has_status("勾魂"), "失败的占位路径不得清除已生效 debuff"
