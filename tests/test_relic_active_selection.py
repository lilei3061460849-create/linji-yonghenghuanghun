"""遗物启用归类（2026-10-22 用户令）：持有不限，生效数受[血限]承载，[战始]自定义启用。

用户原例：持有 5 件遗物、[血限]60（承载2），第一回合任意选 2 件生效。
设计口径：优先归类归纳，如无必要不造新物——启用/停用只是既有 Relic 对象上
的状态（`Relic.enabled`），不新增品类、不新增名单；判定归口
`GameState._relic_active` / `set_active_relics`，与【封印】并列。

这里钉五件事：
1. 局外 set_active_relics 自定义启用（选哪几件、校验件数与持有）；
2. battle_start 的 active_relics 在第一回合选定本场生效集合；
3. 停用遗物不生效（_relic_active 口径，含守夜灯的实际结算入口）；
4. 新获得遗物：生效数未满默认启用，满了先停用（grant_relic 归口）；
5. 启用状态随存档往返不丢失。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Relic
from tests.setup_support import finish_initial_daowen

FIVE = ["守夜灯", "承露盏", "避风铃", "同魂笔", "不离"]


def _engine():
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="ras"), "g.db"),
                   rng_seed=7, save_dir=tempfile.mkdtemp(prefix="ras2"))
    e.execute_action("setup_attributes", {"name": "选器者", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    if setup.get("result", {}).get("relic_choices"):
        e.execute_action("choose_discovered_relic",
                         {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _give_five(e):
    """给玩家 5 件遗物并把血限设为 60（承载 2 件生效）。"""
    e.state.relics = [Relic(name, "") for name in FIVE]
    e.state.player.blood_limit = 60
    e.state.player.current_hp = 60
    assert e.state.player.relic_capacity == 2


# ==================== 1. 局外自定义启用 ====================

def test_set_active_relics_picks_any_two_of_five():
    """正常路径：5 件持有、承载 2，任意选 2 件生效，其余停用但仍持有。"""
    e = _engine()
    _give_five(e)

    r = e.execute_action("set_active_relics", {"relic_names": ["避风铃", "不离"]})

    assert r["success"] is True, r
    assert r["result"]["enabled"] == ["不离", "避风铃"], r["result"]
    assert r["result"]["disabled"] == ["同魂笔", "守夜灯", "承露盏"], r["result"]
    assert {x.name for x in e.state.relics} == set(FIVE), "停用不得丢弃，5 件全持有"
    assert e.state.enabled_relic_names() == {"避风铃", "不离"}


def test_set_active_relics_over_capacity_is_rejected_without_changes():
    """边界：提交 3 件超过承载 2 → 拒绝，且启用集合保持原样。"""
    e = _engine()
    _give_five(e)
    e.execute_action("set_active_relics", {"relic_names": ["避风铃", "不离"]})

    r = e.execute_action("set_active_relics",
                         {"relic_names": ["避风铃", "不离", "守夜灯"]})

    assert r["success"] is False, r
    assert "超出承载" in r["error"], r["error"]
    assert e.state.enabled_relic_names() == {"避风铃", "不离"}, "失败提交不得改动启用集合"


def test_set_active_relics_unknown_name_is_rejected():
    """错误输入：提交未持有的遗物名 → 拒绝。"""
    e = _engine()
    _give_five(e)

    r = e.execute_action("set_active_relics", {"relic_names": ["根本不存在的遗物"]})

    assert r["success"] is False
    assert "未持有" in r["error"], r["error"]


def test_set_active_relics_empty_list_disables_all():
    """边界：空列表合法——全部停用（仍持有）。"""
    e = _engine()
    _give_five(e)

    r = e.execute_action("set_active_relics", {"relic_names": []})

    assert r["success"] is True, r
    assert e.state.enabled_relic_names() == set()
    assert len(e.state.relics) == 5


# ==================== 2. 战始第一回合选定 ====================

def test_battle_start_active_relics_selects_for_this_battle():
    """正常路径：battle_start 提交 active_relics，本场只让选中的 2 件生效。"""
    e = _engine()
    _give_five(e)
    e.state.energy = 0

    r = e.execute_action("battle_start", {"active_relics": ["守夜灯", "同魂笔"]})

    assert r["success"] is True, r
    assert e.state.enabled_relic_names() == {"守夜灯", "同魂笔"}
    player = e.state.player
    assert e.state._relic_active(player, "守夜灯") is True
    assert e.state._relic_active(player, "避风铃") is False, "未选中的遗物本场不得生效"


def test_battle_start_active_relics_over_capacity_fails_atomically():
    """边界：战始提交 3 件超过承载 2 → 整场不开始，启用集合不变。"""
    e = _engine()
    _give_five(e)
    e.state.energy = 0
    phase_before = e.state.phase

    r = e.execute_action("battle_start",
                         {"active_relics": ["守夜灯", "避风铃", "不离"]})

    assert r["success"] is False, r
    assert "超出承载" in r["error"], r["error"]
    assert e.state.phase == phase_before, "失败的战始不得进入战斗"
    assert e.state.enabled_relic_names() == set(FIVE), "回滚后启用集合不得被改动"


# ==================== 3. 停用即不生效 ====================

def test_disabled_relic_does_not_trigger_its_effect():
    """正常路径：守夜灯停用时[回终]法力结算入口读到 0，启用后恢复。"""
    e = _engine()
    player = e.state.player
    e.state.relics = [Relic("守夜灯", "", enabled=False)]

    assert e.combat._relic_active(player, "守夜灯") is False
    assert e.combat._shouyedeng_grant_amount(player) == 0, "停用遗物不得结算效果"

    e.state.current_round = 3
    e.state.relics[0].enabled = True
    assert e.combat._relic_active(player, "守夜灯") is True
    assert e.combat._shouyedeng_grant_amount(player) == 3, "启用后按当前回合数授予"


def test_disabled_stacks_with_sealed_not_replacing_it():
    """边界：封印与停用是两种并列的「持有但不生效」状态，互不顶替。"""
    e = _engine()
    player = e.state.player
    e.state.relics = [Relic("守夜灯", "")]
    e.state.sealed_relics["守夜灯"] = 1
    assert e.state._relic_active(player, "守夜灯") is False, "封印中不生效（原口径）"

    e.state.sealed_relics.clear()
    assert e.state._relic_active(player, "守夜灯") is True, "解封后恢复生效"


# ==================== 4. 获得时的默认归类 ====================

def test_grant_relic_auto_enables_until_capacity_then_holds_disabled():
    """正常路径：获得入口的归口 note_relic_gained——承载 2 时连拿 3 件，
    前 2 件默认启用，第 3 件持有但停用；玩家可再自行改选换进来。"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60                       # 承载 2
    e.state.relics = []

    gained = []
    for name in ("守夜灯", "避风铃", "不离"):
        relic = Relic(name, "")
        e.state.relics.append(relic)
        e.state.note_relic_gained(relic)     # 发现/自选/交易/拾取等入口共用此归口
        gained.append(relic)

    assert gained[0].enabled and gained[1].enabled, "生效数未满时默认启用"
    assert gained[2].enabled is False, "生效数已满时新遗物先停用"
    assert len(e.state.relics) == 3, "持有不限：第 3 件不得被拒"
    # 玩家随后可自行改选：把第 3 件换进来
    ok, err = e.state.set_active_relics(["守夜灯", "不离"])
    assert ok, err
    assert e.state.enabled_relic_names() == {"守夜灯", "不离"}


def test_direct_grant_stays_enabled_regardless_of_capacity():
    """沿革口径：grant_relic 是初拥/龙族等「选了即授予」的入口，旧口径绕过
    承载拦截——授予即默认启用，不因承载为 0 被自动停用。"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 18                       # 承载 0
    e.state.relics = []

    granted = e.state.grant_relic("寒冰法力", "", tag="血族")

    assert granted.enabled is True, "授予口不得自动停用（玩家可事后用 set_active_relics 自行停用）"
    assert e.state._relic_active(p, "寒冰法力") is True


# ==================== 5. 存档往返 ====================

def test_enabled_flag_survives_save_load_roundtrip():
    """边界：停用状态写入存档，读档后仍停用（旧档缺字段默认启用由 getattr 兜底）。"""
    e = _engine()
    _give_five(e)
    e.execute_action("set_active_relics", {"relic_names": ["避风铃"]})

    assert e.save_game("ras")["success"]
    assert e.load_game("ras")["success"]

    assert e.state.enabled_relic_names() == {"避风铃"}, "读档后启用集合不得丢失"
    assert {x.name for x in e.state.relics} == set(FIVE)


# ==================== 6. 单件切换行动 ====================

def test_toggle_relic_flips_one_relic_off_and_on():
    """正常路径：toggle 一次停用、再一次启用，只动被点名的那一件。"""
    e = _engine()
    _give_five(e)
    e.execute_action("set_active_relics", {"relic_names": ["避风铃", "不离"]})

    off = e.execute_action("toggle_relic", {"relic_name": "避风铃"})
    assert off["success"] is True, off
    assert off["result"]["enabled"] is False
    assert e.state.enabled_relic_names() == {"不离"}, "只应停用被点名的一件"

    on = e.execute_action("toggle_relic", {"relic_name": "避风铃"})
    assert on["success"] is True, on
    assert on["result"]["enabled"] is True
    assert e.state.enabled_relic_names() == {"避风铃", "不离"}


def test_toggle_relic_on_when_full_is_rejected():
    """边界：生效数已满（2/2）时启用第 3 件被拒，提示先停用一件。"""
    e = _engine()
    _give_five(e)
    e.execute_action("set_active_relics", {"relic_names": ["避风铃", "不离"]})

    r = e.execute_action("toggle_relic", {"relic_name": "守夜灯"})

    assert r["success"] is False, r
    assert "已满承载" in r["error"], r["error"]
    assert e.state.enabled_relic_names() == {"避风铃", "不离"}, "失败切换不得改动状态"


def test_toggle_relic_unowned_is_rejected():
    """错误输入：切换未持有的遗物被拒。"""
    e = _engine()
    _give_five(e)

    r = e.execute_action("toggle_relic", {"relic_name": "根本不存在的遗物"})

    assert r["success"] is False
    assert "未持有" in r["error"], r["error"]
