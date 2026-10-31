"""拆分后多事实源的提取与非法配置校验。"""
from pathlib import Path
import pytest

from engine.rule_sync import RuleSync

ROOT = Path(__file__).resolve().parents[1]


def _sync(tmp_path=None):
    if tmp_path is None:
        return RuleSync(db_path="data/test_rule_sources.db")
    return RuleSync(rule_files=[], rules_dir=str(tmp_path), db_path=str(tmp_path / "sync.db"))


def test_project_rules_are_extracted_from_their_authoritative_documents():
    """正常路径：法术、物品、副本和怪物分别来自裁定后的事实源。"""
    sync = _sync()
    facts = sync.extract_project_rules()
    assert [entry["name"] for entry in facts["event_daowen"]] == ["真理"]
    # 强化进入转化道纹后通用正文16条；副本定义41条，另有龙心谷3条支付待定草案。
    assert len(facts["common_daowen"]) == 16
    # 2026-10-31：共罪回到永夜庭主环并写进副本定义 → 副本专属道纹 44 → 45。
    assert len(facts["dungeon_daowen"]) == 45
    assert len(facts["spells"]) == 10  # 2026-09-16：删「血溅五步」（无引擎流程的空名字）
    assert len(facts["dungeons"]) == 8
    assert len(facts["monsters"]) == 48  # 36 + 乱葬岗12(已实现)
    diff = sync.diff_project_daowen()
    assert diff["in_file_only"] == [], "通用或已实现副本正文不得定义未登记的道纹"
    assert set(diff["draft_only"]) == {"炽热", "熔化", "锻造"}
    # diff 的范围是「通用＋已实现副本」；草案和种族道纹有自己的事实源，不能为使
    # 双向列表为空而被误搬进通用正文。除此之外不允许出现新的 engine-only 缺口。
    from engine.gamedata import RACE_EXCLUSIVE_DAOWEN, UNIMPLEMENTED_DAOWEN
    draft_names = {row["name"] for row in facts["dungeon_daowen"]
                   if row.get("status") != "已实现"} | set(UNIMPLEMENTED_DAOWEN)
    race_names = {name for names in RACE_EXCLUSIVE_DAOWEN.values() for name in names}
    assert set(diff["in_engine_only"]) <= draft_names | race_names

    spell_names = {spell["name"] for spell in facts["spells"]}
    item_names = {item["name"] for item in facts["items"]}
    assert {"先发制人", "咎由自取", "血炼周天", "镇魔印"} <= spell_names
    assert {"血誓戒", "冥婚契约", "归潮梭", "灾厄轮盘"} <= item_names
    assert "遗忘书屋" not in item_names, "事件不得再被误识别为遗物"


def test_draft_rules_are_visible_to_docs_but_not_runtime_monster_source():
    """边界：草案及其物品可被审计，但草案怪物不进入现行怪物源。"""
    facts = _sync().extract_project_rules()
    status = {entry["name"]: entry["status"] for entry in facts["dungeons"]}
    assert status["乱葬岗"] == "已实现"  # 乱葬岗已转已实现
    assert status["巴别塔"] == "未实现"
    assert {monster["region"] for monster in facts["monsters"]} == {
        "扭曲都市", "罪孽都市", "龙心谷", "乱葬岗",
    }


def test_duplicate_or_empty_item_entries_are_rejected(tmp_path):
    """错误输入：重名物品和空效果正文均由提取器拒绝。"""
    sync = _sync(tmp_path)
    duplicate = tmp_path / "duplicate.md"
    duplicate.write_text(
        "# 物品索引\n\n## 遗物\n\n### 重名\n效果甲\n\n### 重名\n效果乙\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复条目"):
        sync.extract_items_from_file("duplicate.md")

    empty = tmp_path / "empty.md"
    empty.write_text("# 物品索引\n\n## 遗物\n\n### 空条目\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少效果正文"):
        sync.extract_items_from_file("empty.md")


def test_spell_without_required_fields_is_rejected(tmp_path):
    """错误输入：缺少所需道纹或生效流程的法术配置必须拒绝。"""
    sync = _sync(tmp_path)
    invalid = tmp_path / "spells.md"
    # 节名必须是「## 法术大全」——rule_sync.extract_spells_from_file 只认这个
    # 标题（真实的 死者之书.md 用的也是它）。旧夹具写成「## 可学法术」，解析器
    # 根本不会进入法术节，于是永远抛不出 ValueError，用例假失败。
    invalid.write_text(
        "# 死者之书\n\n## 法术大全\n\n### 空法术\n\n触发条件：回始\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="缺少所需道纹或生效流程"):
        sync.extract_spells_from_file("spells.md")
