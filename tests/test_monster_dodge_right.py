"""怪物闪避权的契约测试（2026-09-17 用户令）。

此前只有**最终死斗里的轮回者**会闪避（ai_tactics 的 dodgeable 硬要求
target.entity_type == "轮回者"），PvE 怪物从未行使过规则正文《基础定义》的闪避权。

本次改动：给怪物同等的闪避权，**判据与轮回者完全同口径**——
致命击、或单次伤害高于自己的攻击力（闪比反打划算）才花那 1 点速度。
怪物"不一定用"，但必须**有这个可选项**。

闪避权在架构上由**行动方**替目标提交（hits[].dodge），所以这里验证的是
行动方按目标侧口径算出的 dodge 决策，而不是引擎的免伤结算。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.ai_tactics import TacticalAI
from engine.models import Entity

from tests.setup_support import (begin_battle, begin_round, finish_initial_daowen,
                                hand_priority_to_player)


def _engine(tmp_path, seed: int = 20260822):
    from engine.api import GameEngine
    e = GameEngine(db_path=str(tmp_path / "w.db"), save_dir=str(tmp_path / "saves"),
                   sealed_candidate_path=str(tmp_path / "sealed.json"),
                   death_book_path=str(tmp_path / "death.md"), rng_seed=seed)
    assert e.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": "龙心谷"})["success"]
    return e


def _monster(hp=40, power=3, speed=5):
    """脆弱怪：血少、攻低、速度够闪几次。"""
    return Entity("靶怪", "怪物", blood_limit=40, current_hp=hp,
                  attack_count=speed, attack_power=power,
                  speed_limit=speed, current_speed=speed,
                  mana_limit=power, current_mana=power)


def _setup(e, monster, player_power):
    assert begin_battle(e)["success"]
    e.state.enemies = [monster]
    e.state.player.attack_power = player_power
    e.state.player.mana_limit = player_power
    e.state.player.current_mana = player_power
    e.state.player.attack_count = 1
    e.state.player.current_speed = 1
    assert begin_round(e)["success"]
    # 先手通用规则：靶怪速限通常高于轮回者，[回始]优先权归怪物；本文件验的是
    # 行动方替怪物算出的闪避决策，故声明本回合轮到轮回者。
    hand_priority_to_player(e)
    return e


def _submitted_dodges(e, target_name):
    """走 TacticalAI 的普攻两段动作，取它替目标算出的逐击 dodge 决策。"""
    ai = TacticalAI(e)
    steps = ai._attack_steps(target_name)
    prepared = e.execute_action("prepare_attack", steps[0][1])
    if not prepared.get("success"):
        return None, prepared
    payload = steps[1][1](prepared)
    return [h.get("dodge") for h in payload.get("hits", [])], prepared


# ---------------- 判据本身（与轮回者同口径） ----------------

def test_worth_dodging_on_lethal_hit():
    """致命击：这一击会打死自己 → 值得闪。"""
    t = _monster(hp=5, power=3)
    assert TacticalAI._target_worth_dodging(t, per_hit=5, hp_left=5) is True


def test_worth_dodging_when_hit_exceeds_own_power():
    """单次伤害高于自己的攻击力 → 闪比反打划算。"""
    t = _monster(hp=100, power=3)
    assert TacticalAI._target_worth_dodging(t, per_hit=9, hp_left=100) is True


def test_not_worth_dodging_when_tanking_is_cheaper():
    """错误对照：伤害低于自己的攻击力且不致命 → 不闪，留着速度反打。"""
    t = _monster(hp=100, power=20)
    assert TacticalAI._target_worth_dodging(t, per_hit=3, hp_left=100) is False


# ---------------- 怪物真的拿到闪避权 ----------------

def test_monster_dodges_lethal_hit(tmp_path):
    """正常路径：玩家打出致命击时，怪物会花速度闪避。"""
    e = _engine(tmp_path)
    m = _monster(hp=5, power=3, speed=5)      # 玩家攻力 12 ≥ 剩余生命 5 → 致命击
    _setup(e, m, player_power=12)
    dodges, prepared = _submitted_dodges(e, "靶怪")
    assert dodges, f"未能构造出攻击：{prepared}"
    assert any(dodges), f"致命击下怪物应选择闪避，实际 {dodges}"


def test_monster_dodge_spends_speed(tmp_path):
    """代价：闪避每击花 1 点速度，且总闪避次数不超过当前速度。"""
    e = _engine(tmp_path)
    m = _monster(hp=5, power=3, speed=2)      # 速度只够闪 2 次
    _setup(e, m, player_power=12)
    dodges, prepared = _submitted_dodges(e, "靶怪")
    assert dodges, f"未能构造出攻击：{prepared}"
    assert sum(1 for d in dodges if d) <= 2, f"闪避次数超过速度上限：{dodges}"


def test_monster_does_not_dodge_when_not_worth_it(tmp_path):
    """错误对照：伤害不致命且低于自身攻击力 → 怪物不闪，速度不浪费。"""
    e = _engine(tmp_path)
    m = _monster(hp=500, power=50, speed=5)   # 血厚、攻高；玩家攻力 3
    _setup(e, m, player_power=3)
    dodges, prepared = _submitted_dodges(e, "靶怪")
    assert dodges, f"未能构造出攻击：{prepared}"
    assert not any(dodges), f"不划算时不应闪避，实际 {dodges}"


# ---------------- 关闸 ----------------

def test_monster_dodge_can_be_disabled(tmp_path, monkeypatch):
    """边界：LJ_AI_MONSTER_DODGE=0 关闸后怪物不闪，可复现旧行为。"""
    monkeypatch.setenv("LJ_AI_MONSTER_DODGE", "0")
    e = _engine(tmp_path)
    m = _monster(hp=5, power=3, speed=5)      # 本该致命击闪避
    _setup(e, m, player_power=12)
    dodges, prepared = _submitted_dodges(e, "靶怪")
    assert dodges, f"未能构造出攻击：{prepared}"
    assert not any(dodges), f"关闸后怪物不应闪避，实际 {dodges}"
