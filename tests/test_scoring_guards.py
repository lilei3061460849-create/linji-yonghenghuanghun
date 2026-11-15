"""评分守卫测试（报告⑥ 待办，2026-09-10 六审续落地）。

钉住打分层的三个不变量：
1. 自伤不是战术牌（_digest_diff → kind="harm"，杜绝无活敌时 try_buff 拿输出纹打自己）；
2. remove 只认「动作前还活着」的敌人（尸体不再白得 +6.0 向性分）；
3. 法力损耗按攻力等价折价（「法力=攻力」的机会成本进打分，满池梭哈盾为负分）。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.ai_tactics import TacticalAI  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "guards.db"), rng_seed=4)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.state.energy = 0
    e.execute_action("battle_start", {"relic_choices": {}})
    e.execute_action("round_start", {})
    return e


def _base_player_diff():
    return {"hp_before": 42, "hp_after": 42, "bl_before": 42, "bl_after": 42,
            "mana_before": 10, "mana_after": 10, "speed_before": 8, "speed_after": 8,
            "shield_before": 0, "shield_after": 0, "mutation_delta": 0,
            "healed_delta": 0, "status_before": [], "status_after": [], "dead": False}


def test_self_damage_is_harm_not_tactician(engine):
    """自伤 diff 必须归纳为 harm——兜底 tactician 会让 try_buff 拿输出纹打自己。"""
    ai = TacticalAI(engine)
    diff = {"player": dict(_base_player_diff(), hp_after=37),
            "enemies": [], "events": [
                {"type": "damage_applied", "target": "贾凡", "data": {"raw_damage": 5}}]}
    info = ai._digest_diff(diff)
    assert info["kind"] == "harm", info


def test_remove_requires_alive_before(engine):
    """remove 的签名=动作前活着、动作后无伤消失（0→0）。

    带血量位移的击杀走 damage 支（+8 击杀分另行计），不占 remove；
    尸体（动作前已死，0→0）不得白得 remove +6.0 向性分。
    """
    ai = TacticalAI(engine)
    corpse = {"name": "尸霸", "hp_before": 0, "hp_after": 0,
              "dead": True, "alive_before": False}
    diff = {"player": _base_player_diff(), "enemies": [corpse], "events": []}
    info = ai._digest_diff(diff)
    assert info["kind"] != "remove", info          # 尸体白得的 remove 已封死

    vanish = dict(corpse, alive_before=True)       # 无伤消失（封印/放逐签名）
    diff2 = {"player": _base_player_diff(), "enemies": [vanish], "events": []}
    info2 = ai._digest_diff(diff2)
    assert info2["kind"] == "remove", info2        # 真移除照常识别

    hurt_kill = dict(corpse, hp_before=5, alive_before=True)   # 带伤击杀
    diff3 = {"player": _base_player_diff(), "enemies": [hurt_kill], "events": []}
    info3 = ai._digest_diff(diff3)
    assert info3["kind"] == "damage" and info3["dmg"] == 5, info3


def test_alive_before_defaults_to_hp(engine):
    """旧口径 diff（无 alive_before 键）按 hp_before>0 回退——兼容手工构造。

    回退口径下：legacy 尸体（hp 0→0，无键）不算 alive → 不构成 remove；
    legacy 活体击杀（hp 3→0，无键）按活体算——但带血量位移走 damage 支
    （remove 只留给无伤消失，见上条）。此处钉的是回退不误报 remove。
    """
    ai = TacticalAI(engine)
    legacy = {"name": "尸霸", "hp_before": 0, "hp_after": 0, "dead": True}
    diff = {"player": _base_player_diff(), "enemies": [legacy], "events": []}
    assert ai._digest_diff(diff)["kind"] != "remove"


def test_mana_spend_penalized_by_attack_count(engine):
    """同样把法力 10→0 换盾，攻次越高罚越重（法力=攻力的机会成本）。"""
    ai = TacticalAI(engine)
    enemy_ok = {"name": engine.state.enemies[0].name, "hp_before": 100,
                "hp_after": 100, "dead": False, "alive_before": True}
    diff = {"player": dict(_base_player_diff(), mana_after=0, shield_after=10),
            "enemies": [enemy_ok], "events": [],
            "shards_before": 0, "shards_after": 0}

    engine.state.player.current_speed = 2
    ai2 = TacticalAI(engine)
    s_low = ai2._score_candidate(diff, "盾X=10", kind="shield")
    engine.state.player.current_speed = 8
    ai8 = TacticalAI(engine)
    s_high = ai8._score_candidate(diff, "盾X=10", kind="shield")
    assert s_low is not None and s_high is not None
    assert s_high < s_low, (s_low, s_high)         # 攻次 8 折价 > 攻次 2


def test_full_pool_shield_dump_loses_to_damage(engine):
    """低威胁下满池换盾净分必须低于同额度伤害——化雕塑盲区的守卫。

    （高威胁时盾有真实价值，对比只在低威胁面成立——先把敌人攻击归零，
    排除 shield_useful 干扰。注意伤害走**面板位移**差异：两候选的敌人
    面板必须不同，否则双方同拿 1.4×hp_loss。）
    """
    ai = TacticalAI(engine)
    foe = engine.state.enemies[0]
    foe.current_speed = 0
    foe.attack_count = 0          # 威胁归零：盾的「有用溢出」= 0
    enemy_idle = {"name": foe.name, "hp_before": 100, "hp_after": 100,
                  "dead": False, "alive_before": True}
    enemy_hurt = {"name": foe.name, "hp_before": 100, "hp_after": 60,
                  "dead": False, "alive_before": True}
    dump = {"player": dict(_base_player_diff(), mana_after=0, shield_after=10),
            "enemies": [enemy_idle], "events": [],
            "shards_before": 0, "shards_after": 0}
    strike = {"player": dict(_base_player_diff(), mana_after=0),
              "enemies": [enemy_hurt], "events": [
                  {"type": "damage_applied", "target": foe.name,
                   "data": {"raw_damage": 40}}],
              "shards_before": 0, "shards_after": 0}
    s_dump = ai._score_candidate(dump, "盾X=10", kind="shield")
    s_strike = ai._score_candidate(strike, "杀伐X=8", kind="damage")
    assert s_strike > s_dump, (s_dump, s_strike)
    # 攻力折价可见：同样的梭哈，当前攻次 8 比面板攻次 4 多罚 4×0.5×10=20 分
    engine.state.player.current_speed = 8
    ai12 = TacticalAI(engine)
    s_dump12 = ai12._score_candidate(dump, "盾X=10", kind="shield")
    assert s_dump - s_dump12 == pytest.approx(20.0), (s_dump, s_dump12)
