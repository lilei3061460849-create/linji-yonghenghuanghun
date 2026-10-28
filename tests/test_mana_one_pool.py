"""法力一池制（DM裁定 2026-09-09）：一场战斗一池法力。

裁定背景：原口径是「[回始]获得等同[法限]的法力、[敌回终]清空」，等于每回合把蓝条
重置一次；DM 改为一池制——[战始]直接给满，整场战斗按剩余量支配，[战终]复原
（与当前速度同口径）。连带裁定：【勾魂】原「持续X回合无法获得法力」失去作用对象，
改为**目标消耗法力翻倍**。

本文件钉住整条生命周期，避免旧口径以任何形式回流。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity, Relic, StatusEffect  # noqa: E402
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen  # noqa: E402


def _engine(suffix, **points):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=f"/tmp/linji_tests/test_mana_pool_{suffix}.db", rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_mana_pool_s_{suffix}.json")
    r = e.execute_action("setup_attributes", {"name": "白某", **points})
    assert r.get("success"), r
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    assert e.state.phase == "pre_battle", e.state.phase   # 战始只接受局外阶段
    return e


def _finish_round(e):
    e.state.combat_subphase = "await_round_end"
    return e.execute_action("round_end", {})


def _clear_enemies(e):
    """战终结算要求没有存活敌人。"""
    for en in list(e.state.enemies):
        en.current_hp = 0
        en.is_alive = False          # is_alive 是独立字段，不是 current_hp 的派生属性
        assert not en.is_alive


# ==================== 1. [战始]给满一池 ====================

def test_battle_start_grants_full_pool():
    """正常路径：战始当前法力 = 法限（不再是 0）。"""
    e = _engine("start", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    assert p.mana_limit == 5, p.mana_limit      # DM裁定 2026-09-10：2属性点=1法限
    r = begin_battle(e)
    assert r["success"], r
    assert p.current_mana == p.mana_limit, p.current_mana


def test_relic_gain_cannot_exceed_mana_limit():
    """边界：任何来源的法力获得都不得超过[法限]（用户裁定 2026-09-13 全局化）。

    **本条曾是反向禁区（"不得加 clamp"），已于 2026-09-13 被用户明确推翻。**
    旧裁定：只有【不朽之躯】才把当前法力压回[法限]，其余情况超池是合法面板。
    新裁定：所有属性一律不得超过其上限——上限不只是初始值，它同时定义了
    攻次(速度)与攻力(法力)，超池等于凭空突破面板。
    满池时再获得 = 溢出丢弃；花掉之后再获得才补得回来（见下一条）。
    """
    e = _engine("relic", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.current_mana == p.mana_limit
    p.current_mana += 24
    e.combat.clamp_immortal_body(p)
    assert p.current_mana == p.mana_limit, f"满池获得须被压回法限，实{p.current_mana}"


def test_gain_still_refills_a_spent_pool():
    """正常路径：封顶不等于失效——花掉的部分照样能被重新填回来。

    这条是上一条的另一半：如果只测"满池溢出丢弃"，很容易把封顶误实现成
    "获得法力一律无效"。守夜灯/承露盏这类回充手段的价值正在这里。
    """
    e = _engine("refill_cap", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(4) is True
    assert p.current_mana == p.mana_limit - 4
    p.current_mana += 3
    e.combat.clamp_immortal_body(p)
    assert p.current_mana == p.mana_limit - 1, "未满池时获得须如实入账"


# ==================== 2. [回始]不回填 / [回终]不清空 ====================

def test_round_start_does_not_refill():
    """正常路径：花掉的法力不会在回始补回来，也不再产出 mana_refill 条目。"""
    e = _engine("refill", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(p.mana_limit - 1) is True
    assert p.current_mana == 1
    _finish_round(e)
    r = begin_round(e)
    assert r["success"], r
    assert [x for x in r["result"].get("effects", [])
            if x.get("type") == "mana_refill"] == [], "一池制：不得有回填条目"
    assert p.current_mana == 1, f"回始不得回填，实{p.current_mana}"


def test_round_end_keeps_leftover_mana():
    """正常路径：回终与敌回终都不清空，剩余法力跨回合保留。"""
    e = _engine("keep", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(p.mana_limit - 1) is True
    r = _finish_round(e)
    assert r["success"], r
    assert [x for x in r["result"].get("effects", [])
            if x.get("type") == "mana_clear"] == [], "一池制：不得有清空条目"
    assert p.current_mana == 1, f"回终不得清空，实{p.current_mana}"
    begin_round(e)
    assert p.current_mana == 1, f"下一回始仍是1，实{p.current_mana}"


def test_pool_never_refills_across_many_rounds():
    """边界：连走三回合，池子只减不增（除非有遗物/道纹显式给）。"""
    e = _engine("many", blood_points=1, speed_points=8, mana_points=16)
    p = e.state.player
    begin_battle(e)
    pool = p.mana_limit
    assert pool == 8, pool
    _finish_round(e)
    for i in range(3):
        assert p.spend_mana(2) is True, f"第{i + 1}回合应还能付 2"
        _finish_round(e)
        begin_round(e)
        assert p.current_mana == pool - 2 * (i + 1), (i, p.current_mana)
    assert p.spend_mana(3) is False, "只剩 2，付不起 3"


# ==================== 3. [战终]复原（与当前速度同口径） ====================

def test_battle_end_restores_mana_like_speed():
    """正常路径：战终把当前法力与当前速度一起复原到上限。"""
    e = _engine("end", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(p.mana_limit - 1) is True
    p.current_speed = 1
    assert (p.current_mana, p.current_speed) == (1, 1)
    _clear_enemies(e)
    r = e.execute_action("battle_end", {})
    assert r.get("success"), r
    assert p.current_mana == p.mana_limit, f"战终应复原法力，实{p.current_mana}"
    assert p.current_speed == p.speed_limit, f"战终应复原速度，实{p.current_speed}"


def test_battle_end_does_not_grant_mana_to_non_reincarnator():
    """错误输入/对照：朋友不是轮回者，战终不给它复原法力。"""
    e = _engine("friend", blood_points=7, speed_points=8, mana_points=10)
    friend = Entity(name="旁观朋友", entity_type="朋友", blood_limit=20, current_hp=20,
                    mana_limit=10, current_mana=2)
    e.state.friends.append(friend)
    begin_battle(e)
    _clear_enemies(e)
    r = e.execute_action("battle_end", {})
    assert r.get("success"), r
    assert friend.current_mana == 2, f"非轮回者不享受战终复原，实{friend.current_mana}"


# ============ 4. 连带裁定：勾魂 = 无法获得[法力]（2026-10-08 用户令）============
#
# 版本史：旧版「[回始]失去2X法力，持续∞」→ 2026-08-30「持续X回合无法获得法力」
#       → 2026-09-09 因法力改一池制、[回始]不再回填而一度改为「消耗翻倍」
#       → 2026-10-08 用户令：恢复「无法获得[法力]」原意。此时战斗内已有多个
#         法力来源（聚能／储能电池／守夜灯／承露盏／共罪／透支／
#         法术），效果重新有对象。判定位唯一：Entity.can_gain_mana()/gain_mana()。

def test_gouhun_blocks_every_mana_gain_until_it_expires():
    """正常路径：勾魂期间**任何来源**的法力增益都不生效；持续X 走完后恢复。"""
    e = _engine("gouhun", blood_points=1, speed_points=0, mana_points=24)
    p = e.state.player
    begin_battle(e)
    assert p.mana_limit == 12, p.mana_limit
    p.current_mana = 4
    p.add_status(StatusEffect(name="勾魂", value=1, remaining_rounds=2, source="寄骨蝇"))

    # 增益被压死：gain_mana 是唯一入口，勾魂期间恒返回 0
    assert p.can_gain_mana() is False
    assert p.gain_mana(5) == 0
    assert p.current_mana == 4, f"勾魂期间不得获得法力，实{p.current_mana}"
    # 已持有的法力一分不扣，消耗也不翻倍
    assert p.spend_mana(3) is True
    assert p.current_mana == 1, f"勾魂不改消耗倍率，3 点就是 3 点，实剩 {p.current_mana}"

    _finish_round(e)
    _finish_round(e)
    assert not p.has_status("勾魂"), "持续X=2 走完应自然到期"
    assert p.can_gain_mana() is True
    assert p.gain_mana(5) == 5
    assert p.current_mana == 6, f"到期后恢复获得法力，实{p.current_mana}"


def test_gouhun_does_not_touch_mana_on_apply():
    """边界：勾魂只压制增益，挂上/到期都不动当前法力。"""
    e = _engine("gouhun_apply", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    foe = Entity(name="寄骨蝇", entity_type="怪物", blood_limit=40, current_hp=40,
                 attack_count=1, attack_power=1)
    e.state.enemies.append(foe)
    begin_battle(e)
    from engine.daowen import DaoWenEngine
    calc = DaoWenEngine.resolve("勾魂", 3, target=p, caster=foe)
    assert calc.get("no_mana_gain") is True
    assert "mana_cost_multiplier" not in calc, "消耗翻倍已撤销，不该再产出该字段"
    res = e.combat.apply_daowen_effect("勾魂", calc, foe, p)
    entry = next(x for x in res["effects"] if x.get("type") == "gouhun")
    assert entry["no_mana_gain"] is True and entry["duration"] == 3, entry
    assert p.current_mana == p.mana_limit, "挂状态不得动当前法力"


def test_gouhun_blocks_relic_and_action_mana_sources():
    """正常路径：守夜灯/承露盏/聚能/储能电池这些来源同样被压死。

    判定位只有 Entity.gain_mana() 一处，所以新增来源天然受管；本用例锁住
    「任何来源都过这一处」这条契约，防止将来有人直接写 current_mana += n 绕过。
    """
    e = _engine("gouhun_sources", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    p.current_mana = 0
    cap = p.mana_limit          # 增益按[法限]封顶
    assert p.gain_mana(cap + 9) == cap and p.current_mana == cap, "未中勾魂：正常获得且封顶"

    p.current_mana = 0
    p.add_status(StatusEffect(name="勾魂", value=1, remaining_rounds=3, source="寄骨蝇"))
    for source, amount in (("守夜灯", 3), ("承露盏", 2), ("聚能", 5),
                           ("储能电池", 12), ("共罪", 1), ("透支", 4), ("法术", 7)):
        before = p.current_mana
        assert p.gain_mana(amount) == 0, f"{source} 的法力应被勾魂压死"
        assert p.current_mana == before, f"{source} 不得改变当前法力"
