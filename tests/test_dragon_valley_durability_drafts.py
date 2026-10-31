"""龙心谷三条用户道纹草案：只入正文，不得被当成已实装。"""
from pathlib import Path

import pytest

from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, UNIMPLEMENTED_DAOWEN, learnability_error

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("炽热", "熔化", "锻造")


def test_three_user_drafts_are_in_dragon_valley_text_and_index():
    dungeon = (ROOT / "副本/龙心谷.md").read_text(encoding="utf-8")
    assert "1.炽热X：支付待定。使[目标]所有消耗品当前耐久-X，最低为0。" in dungeon
    assert "2.熔化X：支付待定。销毁[目标]所有当前耐久≤X的消耗品。" in dungeon
    assert "3.锻造X：支付待定。发现一个消耗品，给予其X耐久，持续∞。" in dungeon

    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    assert "当前索引收录道纹（54种）" in index
    assert "（含3种尚未接入运行时的草案节点）" in index
    for name in NAMES:
        assert f"### {name}\n" in index


def test_drafts_have_no_runtime_registration_or_resonance_edges():
    DaoWenEngine.register_all()
    assert len(DaoWenEngine._registry) == 51
    assert REGION_EXCLUSIVE_DAOWEN["龙心谷"] == set()
    for name in NAMES:
        assert name not in DaoWenEngine._registry
        assert UNIMPLEMENTED_DAOWEN[name] == "龙心谷"
        assert ResonanceEngine.get_available_resonance(name) == []
        with pytest.raises(ValueError, match="未知道纹"):
            DaoWenEngine.resolve(name, 1)
        error = learnability_error(
            name, owned=set(), region="龙心谷", known=DaoWenEngine.list_all())
        assert error and "尚未接入运行时" in error


def test_forge_is_limited_to_existing_fixed_consumables():
    dungeon = (ROOT / "副本/龙心谷.md").read_text(encoding="utf-8")
    assert "【锻造】只能发现既有固定规格消耗品，不接受自定义物品名称或效果" in dungeon
    assert "X只用于所发现物品的耐久" in dungeon


def test_rejected_derived_designs_did_not_enter_dragon_valley():
    dungeon = (ROOT / "副本/龙心谷.md").read_text(encoding="utf-8")
    for name in ("焚时", "复原", "蚀用", "熔韵", "铸物", "熔尽"):
        assert f"{name}X" not in dungeon
    assert "维修重做" not in dungeon
