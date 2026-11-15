"""遗物启用槽战斗中切换（2026-11-10 裁定）。

规则：启用槽 `ceil(血限/30)` 的整组改选（set_active_relics）与单件切换
（toggle_relic）在**战斗中也可执行**——不占出手、不结束回合；受交替出手
约束（轮到你才能改你的），启用件数受同一上限约束；操作者按当前出手侧解析
（敌方回合＝敌方轮回者，操作 state.opponent_relics）。

运行：python -m pytest tests/test_relic_combat_toggle.py -v
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, Relic
from tests.setup_support import finish_initial_daowen


def _engine(suffix: str) -> GameEngine:
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=tempfile.mktemp(prefix=f"relic_toggle_{suffix}_", suffix=".db"),
                   rng_seed=7)
    r = e.execute_action("setup_attributes", {"name": "换装师", "blood_points": 1,
                                              "speed_points": 8, "mana_points": 6})
    assert r["success"], r
    choices = list(e.state.pending_relic_choices)
    e.execute_action("choose_discovered_relic", {"relic_name": choices[0]})
    finish_initial_daowen(e)
    # 血限提到66 → 启用槽 ceil(66/30)=3，方便编排多件场景
    e.state.player.blood_limit = 66
    e.state.player.current_hp = 66
    return e


def _arm_relics(e, names):
    e.state.relics.extend(Relic(name=n, effect="") for n in names)


def test_toggle_in_combat_is_free_and_capacity_bound():
    """正常+边界：战斗中单件切换不占出手、不交优先权；超上限拒绝。"""
    e = _engine("toggle")
    _arm_relics(e, ["甲遗物", "乙遗物", "丙遗物", "丁遗物"])
    assert e.state.set_active_relics(["甲遗物"])[0]
    p = e.state.player
    assert p.enabled_relic_limit == 3

    e.state.phase = "in_combat"
    e.state.current_round = 1
    e.state.duel_turn = "player_side"
    p.actions_used_this_round = 0

    r = e.execute_action("toggle_relic", {"relic_name": "乙遗物"})
    assert r["success"], r
    assert r["result"]["enabled"] is True
    assert r["result"]["actor"] == "换装师"
    # 不占出手、不交优先权
    assert p.actions_used_this_round == 0
    assert e.state.duel_turn == "player_side"
    # toggle＝翻转一件：甲原本启用，乙新启用 → 两件同启（2/3槽）
    assert {x.name for x in e.state.relics if getattr(x, "enabled", True)} == {"甲遗物", "乙遗物"}

    # 第3件后满槽：启用第4件被拒
    r_fill = e.execute_action("toggle_relic", {"relic_name": "丙遗物"})
    assert r_fill["success"], r_fill
    r2 = e.execute_action("toggle_relic", {"relic_name": "丁遗物"})
    assert not r2["success"] and "上限3件" in r2["error"]

    # 停用永远允许
    r3 = e.execute_action("toggle_relic", {"relic_name": "乙遗物"})
    assert r3["success"] and r3["result"]["enabled"] is False


def test_set_active_relics_full_swap_in_combat():
    """正常：战斗中整组改选一次到位，仍是同一槽位上限。"""
    e = _engine("swap")
    _arm_relics(e, ["甲遗物", "乙遗物", "丙遗物", "丁遗物"])
    assert e.state.set_active_relics(["甲遗物"])[0]
    e.state.phase = "in_combat"
    e.state.current_round = 1
    e.state.duel_turn = "player_side"

    r = e.execute_action("set_active_relics", {"active_relics": ["乙遗物", "丙遗物"]})
    assert r["success"], r
    assert set(r["result"]["enabled"]) == {"乙遗物", "丙遗物"}
    assert r["result"]["limit"] == 3
    assert e.state.player.actions_used_this_round == 0

    over = e.execute_action("set_active_relics",
                            {"active_relics": ["甲遗物", "乙遗物", "丙遗物", "丁遗物"]})
    assert not over["success"] and "不得超过3件" in over["error"]


def test_toggle_blocked_on_opponent_turn_and_routes_to_enemy_reincarnator():
    """边界：敌方回合玩家不能改；敌方轮回者轮到时改的是 opponent_relics。"""
    e = _engine("sides")
    _arm_relics(e, ["甲遗物"])
    e.state.set_active_relics(["甲遗物"])
    foe = Entity(name="敌轮", entity_type="轮回者", blood_limit=60, current_hp=60,
                 attack_count=1, attack_power=1)
    e.state.enemies[:] = [foe]
    e.state.opponent_relics.extend(Relic(name="彼遗物", effect="") for _ in range(1))
    e.state.opponent_relics[0].enabled = False
    e.state.phase = "in_combat"
    e.state.current_round = 1

    # 敌方回合：操作者＝敌方轮回者——动玩家遗物＝「未持有」，动敌方遗物＝成功
    e.state.duel_turn = "opponent_side"
    r = e.execute_action("toggle_relic", {"relic_name": "甲遗物"})
    assert not r["success"] and "未持有遗物" in r["error"]

    r2 = e.execute_action("toggle_relic", {"relic_name": "彼遗物"})
    assert r2["success"], r2
    assert r2["result"]["actor"] == "敌轮"
    assert e.state.opponent_relics[0].enabled is True
    jia = next(r for r in e.state.relics if r.name == "甲遗物")
    assert jia.enabled is True, "玩家的启用集合不受影响"

    # 己方回合：操作者切回玩家
    e.state.duel_turn = "player_side"
    r3 = e.execute_action("toggle_relic", {"relic_name": "甲遗物"})
    assert r3["success"] and r3["result"]["enabled"] is False
    assert r3["result"]["actor"] == "换装师"


def test_toggle_rejected_during_pending_attack_and_setup():
    """边界：攻击待提交与开局阶段都不允许整理。"""
    e = _engine("gates")
    _arm_relics(e, ["甲遗物"])
    e.state.phase = "in_combat"
    e.state.current_round = 1
    e.state.duel_turn = "player_side"
    e.state.pending_attack = {"token": "x", "options": {}, "hit_index": 0}
    r = e.execute_action("toggle_relic", {"relic_name": "甲遗物"})
    assert not r["success"] and "待提交的攻击" in r["error"]
    e.state.pending_attack = {}

    e.state.phase = "setup"
    r2 = e.execute_action("toggle_relic", {"relic_name": "甲遗物"})
    assert not r2["success"] and "局外阶段或战斗中" in r2["error"]
