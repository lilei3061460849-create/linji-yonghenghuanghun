"""乱葬岗（二阶）专属事件契约测试。

覆盖6事件：纸人冥婚/镇尸棺材钉/悬木红煞/孤坟香案/赶尸栈房/无名将军墓。
正常路径（各选项核心效果）/ 边界（无队友供奉/无员工替身）/ 错误输入（缺参拒绝）。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, Relic


def _engine(suffix: str, with_ally=True) -> GameEngine:
    e = GameEngine(db_path=f"/tmp/test_lz_ev_{suffix}.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.phase = "pre_battle"
    if with_ally:
        e.state.friends.append(Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                                      attack_count=2, attack_power=4))
    return e


def _set_event(e, name):
    e.event_pool.current = name


def _resolve(e, event, option, **extra):
    e.event_pool.current = event
    return e.execute_action("resolve_event", {"event": event, "option_id": option, **extra})


# ---------- 纸人冥婚 ----------

def test_zhiren_option1_grant_relic_and_costs():
    """正常路径：替新郎交拜——流血15+异变3+获冥婚契约。"""
    e = _engine("zh1")
    hp0 = e.state.player.current_hp
    r = _resolve(e, "纸人冥婚", 1)
    assert r["success"], r
    assert e.state.player.current_hp == hp0 - 15
    assert any(x.name == "冥婚契约" for x in e.state.relics)
    assert any("异变3" in a for a in r["result"]["applied"])


def test_zhiren_option2_shards_and_dodge_block():
    """正常路径：抢撒殡钱——萎缩2+25碎片+下一场无法闪避。"""
    e = _engine("zh2")
    s0 = e.state.shards
    r = _resolve(e, "纸人冥婚", 2)
    assert r["success"], r
    assert e.state.shards == s0 + 25
    assert e.state.event_modifiers.get("next_battle_no_dodge") is True


# ---------- 镇尸棺材钉 ----------

def test_zhenshi_option1_consumable():
    """正常路径：拔出镇魂钉——枯竭3+获镇魂铁钉(耐久3)。"""
    e = _engine("zs1")
    r = _resolve(e, "镇尸棺材钉", 1)
    assert r["success"], r
    item = next(c for c in e.state.consumables if c.name == "镇魂铁钉")
    assert item.current_uses == 3


def test_zhenshi_option2_memory_and_resonance():
    """正常路径：贴符加固——失忆1(杀伐)+获反转残韵+15碎片。"""
    e = _engine("zs2")
    assert "杀伐" in e.state.player.dao_wen
    res0 = e.state.resonance.get("反转", 0)
    r = _resolve(e, "镇尸棺材钉", 2, daowen_names=["杀伐"])
    assert r["success"], r
    assert "杀伐" not in e.state.player.dao_wen, "失忆应移除杀伐"
    assert e.state.resonance.get("反转", 0) == res0 + 1


def test_zhenshi_option2_missing_daowen_rejected():
    """错误输入：贴符加固未提交失忆道纹被拒。"""
    e = _engine("zs3")
    r = _resolve(e, "镇尸棺材钉", 2)
    assert not r["success"]
    assert "daowen_names" in r["error"]


# ---------- 悬木红煞 ----------

def test_xuanmu_option1_lose_ally_for_tishenghost():
    """正常路径：许诺替身——失去朋友+获替死鬼。"""
    e = _engine("xm1")
    n_friends = len(e.state.friends)
    r = _resolve(e, "悬木红煞", 1)
    assert r["success"], r
    assert len(e.state.friends) == n_friends - 1, "应失去一名朋友"
    assert any(c.name == "替死鬼" for c in e.state.consumables)


def test_xuanmu_option1_no_ally_pay_blood_limit():
    """边界：无员工/朋友时，许诺替身改为自身血限-10。"""
    e = _engine("xm2", with_ally=False)
    bl0 = e.state.player.blood_limit
    r = _resolve(e, "悬木红煞", 1)
    assert r["success"], r
    assert e.state.player.blood_limit == bl0 - 10
    assert any(c.name == "替死鬼" for c in e.state.consumables)


def test_xuanmu_option2_hongsha_on_shaifa():
    """正常路径：割血点唇——流血18+杀伐获红煞。"""
    e = _engine("xm3")
    hp0 = e.state.player.current_hp
    r = _resolve(e, "悬木红煞", 2)
    assert r["success"], r
    assert e.state.player.current_hp == hp0 - 18
    assert e.state.player.dao_wen["杀伐"].sha_qi == "红煞"


# ---------- 孤坟香案 ----------

def test_gufen_option1_consumable():
    """正常路径：上前续香——衰老6+获消耗品三香通冥（耐久3/3）。

    【三香通冥】由遗物改消耗品，不再进遗物列表。
    """
    e = _engine("gf1")
    bl0 = e.state.player.blood_limit
    r = _resolve(e, "孤坟香案", 1)
    assert r["success"], r
    assert e.state.player.blood_limit == bl0 - 6
    assert not any(x.name == "三香通冥" for x in e.state.relics)
    cup = next(x for x in e.state.consumables if x.name == "三香通冥")
    assert cup.current_uses == cup.max_uses == 3


def test_gufen_option2_shards():
    """正常路径：踢翻香炉——萎缩2+30碎片。"""
    e = _engine("gf2")
    s0 = e.state.shards
    r = _resolve(e, "孤坟香案", 2)
    assert r["success"], r
    assert e.state.shards == s0 + 30


# ---------- 赶尸栈房 ----------

def test_ganshi_option1_consumable():
    """正常路径：摇动赶尸铃——流血12+疲惫2+获赶尸铃。"""
    e = _engine("gs1")
    r = _resolve(e, "赶尸栈房", 1)
    assert r["success"], r
    assert any(c.name == "赶尸铃" for c in e.state.consumables)


def test_ganshi_option2_huangfu():
    """正常路径：剥取黄符——枯竭X+获X张黄符。"""
    e = _engine("gs2")
    r = _resolve(e, "赶尸栈房", 2, x=2)
    assert r["success"], r
    huangfu = [c for c in e.state.consumables if c.name == "黄符"]
    assert len(huangfu) == 2


def test_ganshi_option2_missing_x_rejected():
    """错误输入：剥取黄符未提交x被拒。"""
    e = _engine("gs3")
    r = _resolve(e, "赶尸栈房", 2)
    assert not r["success"]
    assert "x" in r["error"]


# ---------- 无名将军墓 ----------

def test_wuming_option1_bingsha():
    """正常路径：拔戟试锋——流血20+指定道纹获兵煞。"""
    e = _engine("wm1")
    hp0 = e.state.player.current_hp
    r = _resolve(e, "无名将军墓", 1, daowen_name="杀伐")
    assert r["success"], r
    assert e.state.player.current_hp == hp0 - 20
    assert e.state.player.dao_wen["杀伐"].sha_qi == "兵煞"


def test_wuming_option1_missing_daowen_rejected():
    """错误输入：拔戟试锋未指定道纹被拒。"""
    e = _engine("wm2")
    r = _resolve(e, "无名将军墓", 1)
    assert not r["success"]
    assert "daowen_name" in r["error"]


def test_wuming_option2_armor_to_ally():
    """正常路径：供奉——失去20碎片+队友获重甲兵躯（新版不附带血限+15）。"""
    e = _engine("wm3")
    ally = e.state.friends[0]
    bl0 = ally.blood_limit
    r = _resolve(e, "无名将军墓", 2)
    assert r["success"], r
    assert any(x.name == "重甲兵躯" for x in ally.relics)
    assert ally.blood_limit == bl0  # 2026-11-07 新版：只发遗物本体，不改面板
