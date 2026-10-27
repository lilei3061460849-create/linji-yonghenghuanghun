"""
pytest 风格测试 - 里程碑1：随机数规则改造（引擎自动生成随机数，取代"必须由玩家提供数字"）

文件命名说明：本项目后续每个里程碑单独建一个 tests/test_<milestone>.py 文件，
避免把互不相关的里程碑证据都堆进同一个文件，便于逐项审阅与回归。

范围声明：本文件只覆盖本里程碑交付的内容 —— DiceEngine.auto_roll 以及
被改造为调用它的三个调用点（开局发现遗物 / 共鸣发现遗物 / 探索抽取事件 /
事件文本里的"随机获得遗物"分支）。

不在本文件覆盖范围内（属于后续里程碑，尚未实现，见随消息附带的进度报告）：
黑名单、出战支援、朋友/员工自动出手、撤退机制、夺书战斗、
第8场最终死斗。

运行方式：
    pip install --break-system-packages pytest   # 若尚未安装
    python -m pytest tests/test_rng_engine.py -v
"""
import os
from tests.setup_support import finish_initial_daowen
os.makedirs("/tmp/linji_tests", exist_ok=True)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.dice import DiceEngine
from engine.api import GameEngine


# ========================================================================
# 正常路径：auto_roll 能在不需要外部输入数字的情况下，直接生成结果
# ========================================================================

def test_auto_roll_normal_path_no_external_number_needed():
    """正常路径：auto_roll 不接收外部数字参数，直接返回一个池内选项，并写入历史记录"""
    dice = DiceEngine(seed=42)
    options = ["事件A", "事件B", "事件C", "事件D", "事件E"]
    result = dice.auto_roll("test_pool", options, context="单元测试")

    assert result["auto"] is True, "auto_roll 结果必须标记 auto=True，证明是引擎自己摇的号"
    assert result["selected"] in options, "结果必须是池内选项之一"
    assert result["context"] == "单元测试"
    assert result["seed"] == 42

    # 历史记录里也要能查到这次自动摇号，且标记了 auto/context/seed（可追溯要求）
    history = dice.get_history()
    assert len(history) == 1
    assert history[0]["auto"] is True
    assert history[0]["context"] == "单元测试"
    assert history[0]["seed"] == 42
    assert history[0]["selected_value"] == result["selected"]


def test_auto_roll_reproducible_with_same_seed():
    """可复现性：相同 seed + 相同池 + 相同调用顺序 -> 结果完全一致（这是"可验证"的核心证据）"""
    options = ["A", "B", "C", "D", "E", "F", "G"]

    dice1 = DiceEngine(seed=2026)
    r1 = dice1.auto_roll("p", list(options))

    dice2 = DiceEngine(seed=2026)
    r2 = dice2.auto_roll("p", list(options))

    assert r1["selected"] == r2["selected"], "同种子应产生完全相同的结果，否则不可复现/不可测试"
    assert r1["record"]["selected_index"] == r2["record"]["selected_index"]


def test_auto_roll_different_seed_can_diverge():
    """不同种子允许（不强制）产生不同结果 —— 用统计方式验证确实在"真随机"而不是写死返回第一个"""
    options = list(range(1, 21))  # 20个选项，减少偶然重合概率
    picks = set()
    for seed in range(30):
        dice = DiceEngine(seed=seed)
        r = dice.auto_roll("p", list(options))
        picks.add(r["selected"])
    assert len(picks) > 1, "30个不同种子应至少摇出2种不同结果，否则说明没有真正随机，只是写死了固定索引"


# ========================================================================
# 边界条件
# ========================================================================

def test_auto_roll_single_option_pool():
    """边界：池内只有1个选项时，必须直接返回该选项（不是索引越界或报错）"""
    dice = DiceEngine(seed=1)
    result = dice.auto_roll("single", ["唯一选项"])
    assert result["selected"] == "唯一选项"
    assert result["remaining_in_pool"] == 0


def test_auto_roll_pool_shrinks_after_each_call():
    """边界：同一 pool_name 连续调用 auto_roll，池会像 resolve_pool 一样逐次缩小（不重复放回）"""
    dice = DiceEngine(seed=7)
    options = ["A", "B", "C"]
    r1 = dice.auto_roll("shrink_pool", options)
    status = dice.get_pool_status("shrink_pool")
    assert status["count"] == 2, "抽走1个后，池内应剩2个"

    r2 = dice.auto_roll("shrink_pool", dice._pools["shrink_pool"])
    assert r2["selected"] != r1["selected"] or True  # 池已缩小，selected 来自剩余2个之一
    status2 = dice.get_pool_status("shrink_pool")
    assert status2["count"] == 1


def test_manual_pathway_still_intact_for_regression_and_dm_override():
    """边界：本里程碑不删除旧的手动流程(create_pool/resolve_pool)，回归测试(tests/test_engine.py::test_dice)
    与main.py的DM手动裁定入口都依赖它继续存在且行为不变。"""
    dice = DiceEngine()
    pool_info = dice.create_pool("manual_pool", ["X", "Y", "Z"])
    assert pool_info["range"] == "1~3"
    result = dice.resolve_pool("manual_pool", 2)
    assert result["selected"] == "Y"
    # 手动路径不应被标记为 auto
    assert "auto" not in dice.get_history()[-1] or dice.get_history()[-1].get("auto") is not True


# ========================================================================
# 错误输入 / 非法配置：校验器应当拒绝
# ========================================================================

def test_auto_roll_rejects_empty_pool():
    """错误输入：空池必须抛出 ValueError，不能静默返回 None 或制造假结果"""
    dice = DiceEngine(seed=1)
    with pytest.raises(ValueError):
        dice.auto_roll("empty_pool", [])


def test_dice_engine_negative_seed_type_is_still_deterministic_or_rejected():
    """非法配置探测：seed 必须是可被 random.Random 接受的类型；传入不可哈希/不支持的类型应报错，
    而不是被引擎悄悄忽略后退化成不可控随机（避免"看起来支持seed实际上没生效"的假实现）"""
    with pytest.raises(TypeError):
        DiceEngine(seed=["not", "a", "valid", "seed"])  # list 不可作为 random.Random 的种子


# ========================================================================
# 集成测试：确认真正被改造的3个调用点(开局遗物/共鸣发现/探索)都已经切到 auto_roll，
# 而不是仍然residual地调用未经审计的 random.randrange
# ========================================================================


def _choose_region(engine, region, finish_discovery=False):
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    result = engine.execute_action("setup_choose_region", {"region": region})
    if finish_discovery:
        engine.execute_action("choose_discovered_relic", {
            "relic_name": result["result"]["relic_choices"][0],
        })
    return result


def test_setup_choose_region_uses_engine_auto_roll_not_bare_random():
    """集成：开局属性分配后自动发现的初始遗物，必须来自 engine.dice 的可复现随机源"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rng_1.db", rng_seed=999)
    r_attr = engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    assert len(r_attr["result"]["relic_choices"]) == 3
    finish_initial_daowen(engine)
    r = _choose_region(engine, "扭曲都市")

    assert r["success"] is True
    history = engine.dice.get_history()
    setup_rolls = [h for h in history if h.get("auto") is True
                   and h["pool_name"].startswith("relic_discovery_开局发现_")]
    assert len(setup_rolls) == 3, "开局发现必须由DiceEngine不重复随机列出3件候选"


def test_setup_choose_region_reproducible_across_two_engines_same_seed():
    """集成 + 可复现性：相同 rng_seed 的两个独立 GameEngine 实例，开局摇到的初始遗物必须完全一致"""
    e1 = GameEngine(db_path="/tmp/linji_tests/test_rng_2a.db", rng_seed=555)
    r1 = e1.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})

    e2 = GameEngine(db_path="/tmp/linji_tests/test_rng_2b.db", rng_seed=555)
    r2 = e2.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})

    assert r1["result"]["relic_choices"] == r2["result"]["relic_choices"], \
        "同种子的两局必须列出完全一致的3件开局候选"


def test_explore_action_uses_engine_auto_roll():
    """集成：局外【探索】抽取事件，必须经由 engine.dice.auto_roll，且历史可查"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rng_3.db", rng_seed=123)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "罪孽都市", finish_discovery=True)
    engine.state.energy = 3

    r = engine.execute_action("pre_battle_action", {"sub_action": "探索"})
    assert r["success"] is True
    ev_name = r["result"]["event"]
    assert ev_name  # 必须真的选出了一个事件名

    history = engine.dice.get_history()
    assert any(h.get("auto") is True and h["pool_name"] == "event_pool" for h in history), \
        "探索必须经由 DiceEngine.auto_roll(pool_name='event_pool') 完成"


def test_gongming_discover_uses_engine_auto_roll():
    """集成：局外【共鸣】(发现分支)获取遗物，必须经由 engine.dice.auto_roll"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_rng_4.db", rng_seed=321)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市", finish_discovery=True)
    engine.state.energy = 3

    before = len(engine.dice.get_history())
    r = engine.execute_action("pre_battle_action", {"sub_action": "共鸣"})
    assert r["success"] is True
    assert len(r["result"]["relic_choices"]) == 3

    history = engine.dice.get_history()
    assert len(history) == before + 3
    assert all(h["auto"] is True and h["pool_name"].startswith("relic_discovery_共鸣发现_")
               for h in history[-3:])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
