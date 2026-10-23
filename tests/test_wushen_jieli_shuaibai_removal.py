"""【无神】【借力】【衰败】删除护栏（2026-10-11 用户令）。

用户口径：**先删掉，不做闭环补位、不设计替换道纹**——「在没有整体大改之前，
增加新道路纯粹增加工作量」。因此本文件**不**断言有新路径补上，只断言：
删干净了、留下的空缺是显式登记过的（而不是悄悄漂移）。

留下的空缺（已登记，是否补位待定）：
- 残韵：【全力】的（转换）出边留空（仍有 反转→弱化、曲解→再生）；
- 怪物面板：`tests/test_dungeon_audit.py::PENDING_DW_GAPS` 新增
  乱葬岗/腐疫鼠、乱葬岗/红嫁衣鬼、沉沦海/褪鳞蟹、沉沦海/忘潮祭司；
  沉沦海/潜水员（[员工]面板）道纹清零，不在该审计口径内。
- 机制：【衰败】的 ROUND_START 机制一并删除，回始相位只剩
  洞察·结算(30) → 畸变·标记(60)。

沿用【活血】/【嫁祸】删除的先例：删干净 + 静态护栏，防止旧口径被复活。
"""
from __future__ import annotations

import pathlib

from engine.daowen import DaoWenEngine, ResonanceEngine

ROOT = pathlib.Path(__file__).resolve().parent.parent

DELETED = ("无神", "借力", "衰败")

# 生产代码里不许再出现这三条的运行态读取（注释里的沿革说明不算，故按代码模式校验）
FORBIDDEN_CODE = (
    'has_status("无神")', 'has_status("借力")', 'has_status("衰败")',
    'get_status_value("借力")', 'get_status_value("衰败")',
    "_jieli_boost", "calculate_wushen", "calculate_jieli", "calculate_shuaibai",
    "shuaibai_tick",
)
SCAN = [
    "engine/daowen.py", "engine/api.py", "engine/combat.py", "engine/models.py",
    "engine/battle_report.py", "engine/gamedata.py",
    "engine/combat_parts/daowen_effect.py", "engine/combat_parts/monster_phase.py",
    "engine/combat_parts/spells.py", "engine/mechanisms/builtins.py",
]


def test_three_daowen_are_not_registered():
    """正常路径：注册表里没有这三条，转化道纹名单同步收窄。"""
    DaoWenEngine.register_all()
    for name in DELETED:
        assert name not in DaoWenEngine._registry, f"{name} 仍在注册表里"
    from engine.gamedata import MONSTER_TRANSFORM_DAOWEN

    for name in DELETED:
        assert name not in MONSTER_TRANSFORM_DAOWEN


def test_quanli_transform_edge_is_left_empty_on_purpose():
    """边界：【借力】删除后【全力】的（转换）出边**留空**，不得被悄悄补上新目标。

    这是用户明确口径（不新增道路），所以这里断言的是"空"，不是"有替代"。
    """
    assert ResonanceEngine.find_transformation("全力", "转换") is None
    # 另两条边必须还在
    assert ResonanceEngine.find_transformation("全力", "反转") == "弱化"
    assert ResonanceEngine.find_transformation("全力", "曲解") == "再生"


def test_round_start_phase_no_longer_has_shuaibai():
    """【衰败】的 ROUND_START 机制已删；回始只剩 洞察·结算 → 畸变·标记。"""
    from engine.mechanisms.registry import MECHANISMS
    import engine.mechanisms.builtins  # noqa: F401  （触发注册）

    names = [m.name for m in MECHANISMS.phase_mechanisms("round_start")]
    assert names == ["洞察·结算", "畸变·标记"], names


def test_no_runtime_trace_left_in_production():
    """静态：生产代码不再读取这三条的状态/字段（注释里的沿革说明不算）。"""
    for rel in SCAN:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for token in FORBIDDEN_CODE:
            assert token not in text, f"{rel} 仍含 {token}"
