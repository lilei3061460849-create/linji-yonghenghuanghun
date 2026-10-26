"""承载上限：30X[血限] = 允许拥有 X 种道纹和 X 种遗物（2026-10-12 用户令）。

事实源：`规则正文.md`「承载上限」条（属性与状态规则一节）。
实现：`engine/models.py::Entity.load_capacity`（= `daowen_capacity` = `relic_capacity`），
在 `engine/api.py` 的三个获得入口校验——学习·道纹、选择发现遗物、共鸣·自选，
且都放在扣费之前。

这里钉四件事：
1. 额度公式本身（含不足 30 的余数不折算）；
2. 两个额度独立计数，不互相挤占；
3. 满了再获得被拒绝，且**扣费不发生**（碎片/精力都不动）；
4. 残韵转化是一换一，种类数不变，不受本条影响。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, Relic
from tests.setup_support import finish_initial_daowen


def _grant(entity, name):
    from engine.models import DaoWen, DaoWenInstance
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _fill_to(player, n):
    """把玩家道纹补到 n 种（多的删掉、少的用杀伐闭环里的通用道纹补上）。"""
    for name in list(player.dao_wen)[n:]:
        player.dao_wen.pop(name, None)
    for name in ("杀伐", "再生", "庇护", "束缚", "连击"):
        if len(player.dao_wen) >= n:
            break
        if name not in player.dao_wen:
            _grant(player, name)


def _engine():
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="cap"), "g.db"),
                   rng_seed=7, save_dir=tempfile.mkdtemp(prefix="cap2"))
    e.execute_action("setup_attributes", {"name": "承载者", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    # 局外行动（学习/共鸣）必须走完开局流程才可用
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    if setup.get("result", {}).get("relic_choices"):
        e.execute_action("choose_discovered_relic",
                         {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 5
    return e


# ==================== 1. 额度公式 ====================

def test_capacity_formula_is_blood_limit_over_30():
    """正常路径：额度 = [血限] // 30；不足 30 的余数不折算。"""
    for blood_limit, expected in ((0, 0), (1, 0), (29, 0), (30, 1), (59, 1),
                                  (60, 2), (90, 3), (100, 3), (150, 5)):
        e = Entity("T", "轮回者", blood_limit=blood_limit, current_hp=blood_limit)
        assert e.load_capacity == expected, f"血限{blood_limit} 应为 {expected}"
        assert e.daowen_capacity == expected and e.relic_capacity == expected


def test_capacity_shrinks_when_blood_limit_is_aged_down():
    """边界：血限被压低时额度同步收紧；已超出的部分不被自动没收。"""
    e = Entity("T", "轮回者", blood_limit=90, current_hp=90)
    assert e.load_capacity == 3
    e.blood_limit = 60          # 例如【衰老】压低血限
    assert e.load_capacity == 2, "额度必须跟着血限走"


# ==================== 2. 两个额度独立计数 ====================

def test_daowen_and_relic_quotas_are_independent():
    """正常路径：道纹额度与遗物额度同为 X，但互不挤占。"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 90
    assert p.load_capacity == 3
    # 道纹占满不影响遗物额度
    for name in ("杀伐", "庇护"):
        p.dao_wen.pop(name, None)
    assert len(p.dao_wen) <= 3
    e.state.relics = [Relic("守夜灯", ""), Relic("无所求", ""), Relic("忘忧香", "")]
    assert len({r.name for r in e.state.relics}) == 3 == p.relic_capacity


# ==================== 3. 满了再获得被拒绝，且不扣费 ====================

def test_learn_daowen_over_capacity_is_rejected_without_spending():
    """边界：道纹满了再学习被拒，碎片与精力都不动。"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60                      # 额度 2 种
    _fill_to(p, 2)
    assert p.daowen_capacity == 2 and len(p.dao_wen) == 2

    e.state.shards = 100
    e.state.energy = 5
    before = (e.state.shards, e.state.energy, len(p.dao_wen))
    # 【弱化】是转化道纹、无副本前置条件，能直达容量校验
    r = e.execute_action("pre_battle_action",
                         {"sub_action": "学习", "sub": "daowen", "name": "弱化"})
    assert r["success"] is False, r
    assert "承载" in r["error"] and "血限" in r["error"], r["error"]
    assert e.state.shards == before[0], "容量不足时不得扣碎片"
    assert e.state.energy == before[1], "容量不足时精力应退回"
    assert len(p.dao_wen) == before[2], "容量不足时不得写入道纹"


def test_choose_discovered_relic_over_capacity_is_rejected():
    """边界（2026-10-22 改版）：遗物持有不再被承载拒绝——超额选择成功，
    新遗物持有但停用（生效数已满），待选列表正常清空。"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                      # 生效额度 1 件
    e.state.relics = [Relic("守夜灯", "")]
    e._init_relic_pool()
    choice = next(r.name for r in e.state.relics_pool)
    e.state.pending_relic_choices = [choice]
    e.state.pending_relic_source = "测试"

    r = e.execute_action("choose_discovered_relic", {"relic_name": choice})
    assert r["success"] is True, r
    assert r["result"]["enabled"] is False, "生效数已满，新遗物应先停用"
    assert len(e.state.relics) == 2, "持有不限：新遗物应写入持有"
    assert e.state.enabled_relic_names() == {"守夜灯"}, "只有原先那件在生效"
    assert e.state.pending_relic_choices == [], "选择完成后待选列表应清空"


def test_resonance_self_pick_over_capacity_is_rejected():
    """边界（2026-10-22 改版）：共鸣·自选不再被承载拦——照常扣费拿取，
    新遗物持有但停用（生效数已满）。函数名保留沿革，断言已按新规改写。"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                      # 生效额度 1 件
    e.state.relics = [Relic("守夜灯", "")]
    e._init_relic_pool()
    e.state.shards = 100
    name = next(r.name for r in e.state.relics_pool)

    r = e.execute_action("pre_battle_action", {"sub_action": "共鸣", "tier": 2, "name": name})
    assert r["success"] is True, r
    assert r["result"]["enabled"] is False, "生效数已满，新遗物应先停用"
    assert e.state.shards == 85, "自选照常支付15碎片"
    assert len(e.state.relics) == 2, "持有不限：新遗物应写入持有"
    assert e.state.enabled_relic_names() == {"守夜灯"}, "只有原先那件在生效"


# ==================== 4. 残韵转化不受影响 ====================

def test_resonance_transform_keeps_holder_count_and_stays_within_capacity():
    """正常路径：残韵转化对**持有者**是一换一（种类数不变）；施法者另获转化后道纹。

    实测口径（`engine/api.py::_action_use_resonance`）：`_permanently_convert_daowen`
    把持有者的 source 换成 dest（1→1，不触容量），`_grant_transformed_daowen` 再给
    **施法者**补一份 dest（已持有则不重复）。所以"一换一"只对持有者成立；
    施法者那一份是真正的获得，会不会受承载上限约束属于口径问题（见下条用例）。
    """
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                      # 额度 1 种
    p.dao_wen.clear()
    monster = Entity("M", "怪物", blood_limit=50, current_hp=50)
    _grant(monster, "必中")                 # 残韵转化的是**目标怪物**的道纹
    e.state.enemies = [monster]
    e.state.resonance["反转"] = 1
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"

    r = e.execute_action("use_resonance", {"source_daowen": "必中",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r["success"], r
    assert len(monster.dao_wen) == 1, "持有者一换一，种类数不变"
    assert "蒙蔽" in monster.dao_wen and "必中" not in monster.dao_wen
    # 施法者另获一份【蒙蔽】；血限30 → 额度1，此时刚好等于额度，未超。
    assert list(p.dao_wen) == ["蒙蔽"], "施法者获得转化后道纹"
    assert len(p.dao_wen) <= p.daowen_capacity


def test_resonance_grant_beyond_capacity_is_refused():
    """裁定②（2026-10-12）：施法者已达额度时，残韵转化**不**把转化后道纹补给他。

    持有者的一换一照常发生（那是 1→1，不触容量）；只有"给施法者补一份"这个真正的
    获得受【承载上限】约束，超额就不授予（`_grant_transformed_daowen` 返回 False）。
    """
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                      # 额度 1 种
    _fill_to(p, 1)
    held = list(p.dao_wen)[0]
    monster = Entity("M", "怪物", blood_limit=50, current_hp=50)
    _grant(monster, "必中")
    e.state.enemies = [monster]
    e.state.resonance["反转"] = 1
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"

    r = e.execute_action("use_resonance", {"source_daowen": "必中",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r["success"], r
    assert not r.get("granted_daowen"), f"超额时不应授予：{r.get('granted_daowen')}"
    assert list(p.dao_wen) == [held], f"施法者道纹不得增加：{list(p.dao_wen)}"
    assert "蒙蔽" in monster.dao_wen and "必中" not in monster.dao_wen, "持有者的一换一照常"


def test_setup_phase_is_exempt_from_the_relic_quota():
    """裁定①沿革（2026-10-12 开局豁免 → 2026-10-22 持有不限）：低血限不再需要豁免。

    实测流程（seed=7）：blood_points=3 → 血限18 → 生效额度0；开局遗物照常发下，
    进入局外后再拿一件也不被拒——只是额度0时没有任何遗物能生效（enabled 全 False）。
    """
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="cap0"), "g.db"),
                   rng_seed=7, save_dir=tempfile.mkdtemp(prefix="cap0b"))
    e.execute_action("setup_attributes", {"name": "低血", "blood_points": 3,
                                          "speed_points": 10, "mana_points": 12})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    p = e.state.player
    assert p.blood_limit == 18 and p.relic_capacity == 0, (p.blood_limit, p.relic_capacity)

    region = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    assert region["success"], f"额度0 也必须能走完开局：{region.get('error')}"
    assert len(e.state.relics) == 1, "开局遗物照常发下（豁免）"
    assert e.state.phase == "pre_battle"

    # 2026-10-22：局外再拿一件照常成功（持有不限），额度0 → 两件都不生效
    e._init_relic_pool()
    assert e.state.relics_pool, "遗物池应有候选，否则本断言无意义"
    nxt = e.state.relics_pool[0].name
    e.state.pending_relic_choices = [nxt]
    e.state.pending_relic_source = "测试"
    r2 = e.execute_action("choose_discovered_relic", {"relic_name": nxt})
    assert r2["success"] is True, r2
    assert len(e.state.relics) == 2, "持有不限：第二件应写入持有"
    assert e.state.enabled_relic_names() == set(), "额度0时没有任何遗物能生效"
