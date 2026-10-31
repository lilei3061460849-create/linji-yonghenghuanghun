"""轮回者普攻与加点（DM裁定 2026-09-09 / 2026-09-10）。

2026-09-09：轮回者获得普攻（走既有两阶段攻击接口 prepare_attack/resolve_attack）。
2026-09-10：**换算仅限轮回者**——攻击次数 = 当前速度、攻击力 = 当前法力；
出手次数不再由速限推导，**固定 2 次**；加点改为 2属性点 = 1[速限] = 1[法限]
（1属性点 = 6[血限]），攻次/攻力不再是可购买的独立面板，未花完的属性点存入
属性点池，随时可兑。

于是输出随资源衰减：闪避花掉速度→击数下降，放法力道纹花掉法力→每击伤害下降。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity  # noqa: E402
from tests.attack_support import resolve_attack  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


def _engine(suffix, **points):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=f"/tmp/linji_tests/test_basic_attack_{suffix}.db", rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_basic_attack_s_{suffix}.json")
    r = e.execute_action("setup_attributes", {"name": "白某", **points})
    assert r.get("success"), r
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.state.phase = "in_combat"
    e.state.current_round = 2          # 跳过白板回合
    return e


# ==================== 1. 加点：2属性点 = 1速限 = 1法限 ====================

def test_points_buy_speed_and_mana_at_two_to_one():
    """正常路径：2属性点=1[速限]=1[法限]，1属性点=6[血限]。"""
    e = _engine("rate", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    assert (p.blood_limit, p.speed_limit, p.mana_limit) == (42, 4, 5)
    assert e.state.attribute_points == 0, "25 点花完，池内应为空"


def test_unspent_points_are_banked_not_lost():
    """正常路径（DM裁定 2026-09-10）：没花完的属性点存进池子，随时可兑。"""
    e = _engine("bank", blood_points=6, speed_points=8, mana_points=10)   # 合计 24
    assert e.state.attribute_points == 1
    e.state.phase = "pre_battle"        # 兑换限局外（战斗内兑换会连带补满当前资源）
    r = e.execute_action("redeem_attribute_points",
                         {"allocations": {"speed_points": 0, "mana_points": 0}})
    assert r["success"] and e.state.attribute_points == 1, "0 兑换不该动池子"


def test_over_budget_rejected_and_odd_points_rejected():
    """错误输入：超过 25 点拒绝；速/法按 2 点一档计价，奇数点买不出半档。"""
    e = GameEngine(db_path="/tmp/linji_tests/test_basic_attack_bad.db", rng_seed=7)
    over = e.execute_action("setup_attributes", {
        "name": "白某", "blood_points": 10, "speed_points": 8, "mana_points": 10})  # 28
    assert over["success"] is False and "25" in over["error"] and "28" in over["error"]

    odd = e.execute_action("setup_attributes", {
        "name": "白某", "blood_points": 10, "speed_points": 8, "mana_points": 7})   # 25 但法为奇（本条**故意**非法，勿被批量取偶扫到）
    assert odd["success"] is False and "偶数" in odd["error"]
    assert "池" in odd["instruction"]   # 引擎文案：「余点会留在池里，攒够2点再兑」


# ==================== 2. 攻次/攻力 = 当前速度/当前法力 ====================

def test_attack_panel_is_derived_from_current_resources():
    """正常路径：轮回者的攻次/攻力是派生值，随当前速度/当前法力实时变化。"""
    e = _engine("derived", blood_points=1, speed_points=8, mana_points=16)
    p = e.state.player
    assert (p.speed_limit, p.mana_limit) == (4, 8)
    assert (p.effective_attack_count(), p.effective_attack_power()) == (4, 8)

    p.current_speed = 2                 # 闪避花掉速度 → 击数跟着掉
    p.current_mana = 3                  # 放道纹花掉法力 → 每击伤害跟着掉
    assert (p.effective_attack_count(), p.effective_attack_power()) == (2, 3)


def test_conversion_applies_to_every_character():
    """2026-09-16 用户令：[攻次]=[当前速度]、[攻力]=[当前法力] 对**全体角色**生效。

    旧口径「换算仅限轮回者」已废止——怪物/微光者与轮回者同口径持有[速限]/[法限]，
    "怪物不持有法力"这条旧条文不复存在，换算对它们不再无意义。
    怪物战始当前值给满（故 15 = 由 attack_power 推定的法限），[回始]还会再次回满。
    """
    e = _engine("monster", blood_points=1, speed_points=8, mana_points=16)
    m = Entity(name="尸霸", entity_type="怪物", blood_limit=264, current_hp=264,
               attack_count=5, attack_power=15, speed_limit=6, current_speed=6)
    e.state.enemies.append(m)
    assert (m.effective_attack_count(), m.effective_attack_power()) == (6, 15)
    assert m.current_mana == 15, "怪物战始法力给满=[法限]"


def test_action_count_is_fixed_one():
    """正常路径：所有角色基础出手 1 次（单行动契约），不由速限推导。"""
    for suffix, pts in (("fast", dict(blood_points=1, speed_points=16, mana_points=8)),
                        ("slow", dict(blood_points=17, speed_points=4, mana_points=4))):
        p = _engine(suffix, **pts).state.player
        assert p.action_count == 1, (suffix, p.speed_limit, p.action_count)


# ==================== 3. 普攻落地 ====================

def test_basic_attack_lands_current_speed_times_current_mana():
    """正常路径：普攻总伤 = 当前速度 × 当前法力（目标速度0→不闪避→伤害确定）。"""
    e = _engine("hit", blood_points=1, speed_points=8, mana_points=16)
    p = e.state.player
    m = Entity(name="靶尸", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=1, speed_limit=0, current_speed=0)
    e.state.enemies[:] = [m]
    e.state.phase = "in_combat"
    r = resolve_attack(e)
    assert r.get("success"), r
    expect = p.effective_attack_count() * p.effective_attack_power()   # 4 × 8 = 32
    assert m.current_hp == 200 - expect, (m.current_hp, expect)


def test_spending_mana_weakens_the_basic_attack():
    """正常路径（一池制的取舍）：放一次法力道纹 = 普攻变弱，但回蓝后随时涨回来。

    普攻本身不消耗法力，所以衰减不是自动发生的，而是「用法力」的机会成本；
    攻力是**实时读取当前法力**，不是战始快照——法力补回来（守夜灯/承露盏/
    共罪/透支/再生等）攻击力同步回升，不存在「掉下去就永远回不来」。
    """
    e = _engine("decay", blood_points=1, speed_points=8, mana_points=16)
    p = e.state.player
    before = p.effective_attack_count() * p.effective_attack_power()
    assert p.spend_mana(3) is True                  # 模拟放一次法力道纹
    after = p.effective_attack_count() * p.effective_attack_power()
    assert after == p.effective_attack_count() * 5 == 20 < before
    # 回蓝 2 点（例：守夜灯[回始]授予/承露盏失血返还）→ 攻力跟着回升到 7
    p.current_mana += 2
    assert p.effective_attack_power() == 7
    assert p.effective_attack_count() * p.effective_attack_power() == 28
    # 掉到 0 也不锁死：补满即回到战始水平
    p.current_mana = 0
    assert p.effective_attack_power() == 0
    p.current_mana = p.mana_limit
    assert p.effective_attack_power() == 8


# ==================== 4. 修行：入池 + 兑换 ====================

def test_training_banks_points_and_redemption_raises_attack_panel():
    """正常路径：修行给的点先入池；兑成[速限]/[法限]后攻次/攻力随之上升。"""
    e = _engine("train", blood_points=7, speed_points=8, mana_points=10)
    e.state.phase = "pre_battle"
    e.state.shards = 200
    p = e.state.player
    assert (p.effective_attack_count(), p.effective_attack_power()) == (4, 5)

    r = e.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 4,
        "allocations": {"speed_points": 2, "mana_points": 2}})
    assert r["success"], r
    assert (p.speed_limit, p.mana_limit) == (5, 6)
    assert (p.effective_attack_count(), p.effective_attack_power()) == (5, 6)
    assert r["result"]["gained"] == {"blood": 0, "speed": 1, "mana": 1}
