"""龙心谷【炽热】【熔化】【锻造】（2026-11-02 由草案升运行时）的行为契约。

用户 2026-11-02：「龙心谷的三个道纹直接实装」。三纹原本只有正文定义、支付待定、
不可发动；现在按下列口径接入运行时（支付由实装方按既有草案定价）：
- 炽热X：法力3X，目标所有消耗品当前耐久-X（最低0），保留条目；
- 熔化X：法力5X，销毁目标当前耐久≤X 的条目（含0），切断恢复路径；
- 锻造X：法力5X，从既有固定规格消耗品池发现 3 选 1，领取时耐久按本次 X 覆盖。
"""
from pathlib import Path

import pytest

from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, UNIMPLEMENTED_DAOWEN, learnability_error
from engine.models import Consumable

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("炽热", "熔化", "锻造")


def test_three_daowen_are_written_into_dragon_valley_text_and_index():
    dungeon = (ROOT / "副本/龙心谷.md").read_text(encoding="utf-8")
    assert "3.炽热X：支付：法力3X。使[目标]所有消耗品的当前耐久-X，最低为0。" in dungeon
    assert "2.熔化X：支付：法力5X。销毁[目标]所有当前耐久≤X的消耗品条目（含0耐久）。" in dungeon
    assert "1.锻造X：支付：法力5X。发现一个固定规格消耗品" in dungeon
    assert "支付待定" not in dungeon

    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    assert "本索引收录 87 条道纹" in index
    assert "尚未接入运行时的草案节点" not in index
    for name in NAMES:
        assert f"### {name}\n" in index
        assert f"| {name} | 龙心专属 | 支付：法力" in index


def test_three_daowen_are_registered_runtime_and_learnable_in_region():
    DaoWenEngine.register_all()
    assert len(DaoWenEngine._registry) == 87  # 2026-11-09 第四轮：再生池加【疯长】【自愈】
    assert REGION_EXCLUSIVE_DAOWEN["龙心谷"] == {"锻造", "熔化", "炽热", "淬火", "百炼", "锈蚀", "重铸", "回炉"}
    for name in NAMES:
        assert name in DaoWenEngine._registry
        assert name not in UNIMPLEMENTED_DAOWEN
        calc = DaoWenEngine.resolve(name, 2)
        assert calc["dao_wen"] == name
        assert calc["cost_type"] == "消耗"  # CostType.MANA 的展示名

        # 2026-11-08 最终结构：每纹与本组四面体的另外三个节点相邻。
        group = ({"淬火", "炽热", "百炼", "锈蚀"} if name in ("炽热", "淬火")
                 else {"锻造", "熔化", "回炉", "重铸"})
        outs = {cand["target_daowen"] for cand in ResonanceEngine.get_available_resonance(name)}
        assert outs == group - {name}, outs
    # 学习门禁：先经残韵拿到本副本一种专属道纹，之后可在龙心谷学习。
    known = DaoWenEngine.list_all()
    error = learnability_error("炽热", owned=set(), region="龙心谷", known=known)
    assert error and "残韵" in error
    assert learnability_error("炽热", owned={"淬火"}, region="龙心谷", known=known) is None
    assert "尚未接入运行时" not in learnability_error(
        "炽热", owned={"淬火"}, region="罪孽都市", known=known)


def test_costs_match_the_documented_formulas():
    assert DaoWenEngine.resolve("炽热", 3)["cost"] == 9
    assert DaoWenEngine.resolve("熔化", 3)["cost"] == 15
    assert DaoWenEngine.resolve("锻造", 3)["cost"] == 15
    assert DaoWenEngine.resolve("淬火", 3)["cost"] == 6


def _combat_with_player(suffix):
    """复用 test_daowen_wiring 的战斗夹具；本文件只测三纹的消耗品结算。"""
    from tests.test_daowen_wiring import _engine, _give, _monster
    engine = _engine(f"dragon_{suffix}")
    _give(engine.state.player, "炽热")
    _give(engine.state.player, "熔化")
    _give(engine.state.player, "锻造")
    monster = _monster(engine, "靶怪", hp=100)
    engine.execute_action("round_start", {})
    return engine, engine.state.player, monster


def test_chire_lowers_durability_but_keeps_items():
    engine, player, monster = _combat_with_player("chire")
    monster.consumables = [
        Consumable(name="破障晶", effect="x", current_uses=2, max_uses=2),
        Consumable(name="定命丸", effect="y", current_uses=1, max_uses=2),
    ]
    result = engine.execute_action("use_daowen", {
        "daowen_name": "炽热", "x": 1, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"], result
    dur = {c.name: c.current_uses for c in monster.consumables}
    assert dur == {"破障晶": 1, "定命丸": 0}, dur
    assert len(monster.consumables) == 2, "炽热不销毁条目，只压耐久"


def test_ronghua_destroys_only_items_at_or_below_threshold():
    engine, player, monster = _combat_with_player("ronghua")
    monster.consumables = [
        Consumable(name="破障晶", effect="x", current_uses=2, max_uses=2),
        Consumable(name="定命丸", effect="y", current_uses=1, max_uses=2),
        Consumable(name="灼罪钉", effect="z", current_uses=0, max_uses=1),
    ]
    result = engine.execute_action("use_daowen", {
        "daowen_name": "熔化", "x": 1, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"], result
    assert [c.name for c in monster.consumables] == ["破障晶"]


def test_cuihuo_caps_at_existing_max_durability():
    """淬火只回耐久、不抬上限：超出 max_uses 的部分不计（2026-11-10 取消抬上限）。"""
    engine, player, monster = _combat_with_player("cuihuo_cap")
    from tests.test_daowen_wiring import _give
    _give(engine.state.player, "淬火")
    monster.consumables = [
        Consumable(name="破障晶", effect="x", current_uses=1, max_uses=2),
        Consumable(name="定命丸", effect="y", current_uses=2, max_uses=2),
    ]
    result = engine.execute_action("use_daowen", {
        "daowen_name": "淬火", "x": 3, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"], result
    dur = {c.name: (c.current_uses, c.max_uses) for c in monster.consumables}
    assert dur == {"破障晶": (2, 2), "定命丸": (2, 2)}, dur


def test_duanzao_discovers_only_fixed_spec_consumables_with_x_durability():
    engine, player, monster = _combat_with_player("duanzao")
    result = engine.execute_action("use_daowen", {
        "daowen_name": "锻造", "x": 3,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"], result
    choices = engine.state.pending_item_choices
    assert len(choices) == 3, choices
    from engine.events import EVENT_CONSUMABLES, TWISTED_TOOL_LIBRARY
    fixed = {**TWISTED_TOOL_LIBRARY, **EVENT_CONSUMABLES}
    assert set(choices) <= set(fixed), "锻造只发现既有固定规格消耗品"
    chosen = choices[0]
    picked = engine.execute_action("choose_discovered_item", {"item_name": chosen})
    assert picked["success"], picked
    item = next(c for c in engine.state.consumables if c.name == chosen)
    assert item.current_uses == 3
    assert item.max_uses >= 3
    assert engine.state.pending_item_bonus_uses == 0


def test_forge_is_limited_to_existing_fixed_consumables():
    dungeon = (ROOT / "副本/龙心谷.md").read_text(encoding="utf-8")
    assert "只发现既有固定规格消耗品，不接受自定义物品名称或效果" in dungeon


def test_forge_requires_no_custom_item_name():
    """错误输入：锻造不接受调用方自报物品名或效果，只能走发现候选。"""
    engine, player, monster = _combat_with_player("forge_names")
    result = engine.execute_action("use_daowen", {
        "daowen_name": "锻造", "x": 1, "item_name": "自定义神器", "effect": "秒杀",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"], result
    from engine.events import EVENT_CONSUMABLES, TWISTED_TOOL_LIBRARY
    fixed = {**TWISTED_TOOL_LIBRARY, **EVENT_CONSUMABLES}
    assert "自定义神器" not in engine.state.pending_item_choices
    assert set(engine.state.pending_item_choices) <= set(fixed)


def test_rejected_derived_designs_did_not_enter_dragon_valley():
    dungeon = (ROOT / "副本/龙心谷.md").read_text(encoding="utf-8")
    for name in ("焚时", "复原", "蚀用", "熔韵", "铸物", "熔尽"):
        assert f"{name}X" not in dungeon
    assert "维修重做" not in dungeon
