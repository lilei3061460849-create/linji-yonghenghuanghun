"""护栏：遗物之间不得硬编码联动（`AI_EXPERIENCE.md` 规则 18）。

****：每一个数据只发出信号，遗物接收到信号之后才触发；
不允许为了遗物联动而写死任何硬编码。判据：**把某张遗物整个删掉，
除它自己以外不应有任何代码分支发生变化。**

本文件钉住已被查实的第一个实例：【回锋刀】的战始触发原先挂在
「【苍白之花】是否支付疲惫5」上（`using_fatigue`）。索引里回锋刀只有
「每失去1点速度后」与[回始]两个触发点，该战始触发点已按「按照索引重写程序」删除。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 生产路径（不含 sim/ 与 tests/：它们是隔离的推演工具与测试桩）
PRODUCTION = [
    "engine/combat.py", "engine/api.py", "engine/ai_rules.py",
    "engine/combat_hooks.py", "engine/events.py",
]


def test_no_using_fatigue_cross_relic_gate():
    """静态：不得再用「某张遗物是否付了疲惫」当作另一张遗物的触发条件。"""
    for rel in PRODUCTION:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "using_fatigue" not in text, f"{rel} 又出现了 using_fatigue 跨遗物门控"


def test_pale_flower_is_passive_and_has_no_battle_start_choice():
    """【苍白之花】是被动（当前法力可超出[法限]），战始不再需要提交决策。"""
    combat_src = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")
    assert 'choices["苍白之花"]' not in combat_src, "苍白之花不应再读战始决策"
    assert "pale_flower_active" not in combat_src, "旧的战终精力+1 标记不应再由战斗层写入"

    setup_src = (ROOT / "tests" / "setup_support.py").read_text(encoding="utf-8")
    assert '"苍白之花"' not in setup_src.split("OPTIONAL_BATTLE_START = (")[1].split(")")[0], \
        "苍白之花不应再出现在战始可选遗物名单里"


def test_huifeng_keeps_only_the_two_indexed_triggers():
    """【回锋刀】只保留索引里的两个触发点：失速即时伤 + 回始缺口伤。"""
    combat_src = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")
    # 触发点一：失去当前速度的唯一入口里按失去点数造伤
    assert "_trigger_huifeng_on_speed_loss" in combat_src
    # 触发点二：回始按 (速限-当前速度) 缺口造伤
    assert 'timing": "round_start", "source": "回锋刀"' in combat_src
    # 不得再有战始触发点
    assert 'timing": "battle_start", "source": "回锋刀"' not in combat_src
