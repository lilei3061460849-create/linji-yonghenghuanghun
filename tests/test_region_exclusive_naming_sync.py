"""副本专属道纹的**跨事实源同名守护**（2026-10-12 用户令新增）。

起因：`规则正文.md` 曾把罪孽都市的专属道纹写成【失忆】（消耗8X→80X碎片），而同一提交里
`副本/罪孽都市.md` 与 `全道纹索引.md` 写的是【点金】（代价：失忆X→80X碎片）——
同一条道纹两个名字、两套数值。当时按「正文优先」把全仓统一成了【失忆】，方向反了；
2026-10-12 用户裁定【点金】才是要的那条，已全部回退（见 `报告.md` A16）。

这条守护就是为了让同类冲突**再也走不到"我来统一"这一步**：三处事实源
（`规则正文.md` 的副本专属定义、`副本/<副本>.md` 的闭环与面板、`engine/gamedata.py`
的 `REGION_EXCLUSIVE_DAOWEN`）必须逐副本同名同集合，一旦漂移立刻红，由用户裁定谁对。
"""
from __future__ import annotations

import re
from pathlib import Path

from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

ROOT = Path(__file__).resolve().parents[1]

REGIONS = ("罪孽都市", "扭曲都市", "龙心谷")


def _bullet_daowen(text: str, region: str) -> set[str]:
    """从 `规则正文.md` 的「**<副本>**」小节里抓 `- 【X】` 定义名。"""
    m = re.search(r"\*\*%s\*\*\n(.*?)(?=\n\*\*|\Z)" % re.escape(region), text, re.S)
    assert m, f"规则正文.md 找不到 **{region}** 小节"
    return set(re.findall(r"^- 【([^】]+)】", m.group(1), re.M))


def _dungeon_daowen(region: str) -> set[str]:
    """从 `副本/<副本>.md` 抓闭环串与怪物面板里出现的道纹名。"""
    text = (ROOT / "副本" / f"{region}.md").read_text(encoding="utf-8")
    names: set[str] = set()
    # 闭环串：点金X⇄（转换）典当X⇄…
    for line in text.splitlines():
        if "⇄" in line:
            names |= set(re.findall(r"([\u4e00-\u9fff·]+)X", line))
    # 面板行：名字（血限/攻力/攻次，道纹，道纹…）
    for line in text.splitlines():
        m = re.match(r"^[\u4e00-\u9fff·]+（\d+/\d+/\d+，(.+?)）", line.strip())
        if m:
            names |= {t.strip() for t in re.split(r"[，,、]", m.group(1)) if t.strip()}
    return names


# 2026-10-12 实测登记：这些副本专属道纹在引擎 `REGION_EXCLUSIVE_DAOWEN` 里有、
# 但 `规则正文.md` 的对应副本小节**没有** `- 【X】` 定义。这是既存缺口（不是本次改出来的），
# 我不擅自代写规则定义；先如实登记，等用户裁定补写或改口径。清单一旦变化本测试就会红。
MISSING_RULE_TEXT_DEFINITIONS = {
    "罪孽都市": {"典当", "假钞", "勒索"},   # 2026-10-13：删【逼债】→【典当】顶替；2026-10-14：+【勒索】（定义只在副本文档）
    "扭曲都市": set(),
    "龙心谷": set(),
}


def test_rule_text_daowen_names_match_the_engine_exactly():
    """命名一致性（硬）：`规则正文.md` 里写了 `- 【X】` 的，必须与引擎同名同集合。

    这条就是拦【点金】/【失忆】那类事故的：正文一旦把某条专属道纹写成别的名字，
    立刻红，由用户裁定谁对——**不再由实现方按"正文优先"自行统一全仓**。
    """
    rules_text = (ROOT / "规则正文.md").read_text(encoding="utf-8")
    for region in REGIONS:
        engine_set = set(REGION_EXCLUSIVE_DAOWEN[region])
        in_rules = _bullet_daowen(rules_text, region)
        extra = in_rules - engine_set
        assert not extra, (
            f"{region}：规则正文.md 定义了引擎里没有的专属道纹 {sorted(extra)}"
            f"（改名/新增未同步到 `engine/gamedata.py`？）")


def test_rule_text_definition_coverage_gap_is_exactly_the_registered_list():
    """定义覆盖：正文缺哪些定义必须与登记清单逐字一致（不许悄悄变大或变小）。"""
    rules_text = (ROOT / "规则正文.md").read_text(encoding="utf-8")
    for region in REGIONS:
        engine_set = set(REGION_EXCLUSIVE_DAOWEN[region])
        missing = engine_set - _bullet_daowen(rules_text, region)
        assert missing == MISSING_RULE_TEXT_DEFINITIONS[region], (
            f"{region}：正文缺失定义 {sorted(missing)}，与登记清单 "
            f"{sorted(MISSING_RULE_TEXT_DEFINITIONS[region])} 不符——"
            "补写了就把它从清单里删掉，别让它悄悄漂移")


def test_dungeon_documents_cover_every_region_exclusive_daowen():
    """副本正文（闭环串 + 怪物面板）必须出现每一条本副本专属道纹。"""
    for region in REGIONS:
        expected = set(REGION_EXCLUSIVE_DAOWEN[region])
        in_dungeon = _dungeon_daowen(region)
        missing = expected - in_dungeon
        assert not missing, (
            f"{region}：副本正文里找不到这些专属道纹 {sorted(missing)}"
            f"（闭环串与怪物面板都查过）")


def test_no_stale_daowen_name_leaks_into_the_sin_city_loop():
    """回归：罪孽闭环必须写【点金】，不得再出现【失忆】当道纹名。

    【失忆】只是**代价类型**（`规则正文.md:141`「失忆X：本次轮回永久失去自身拥有的
    X种道纹」），不是道纹名。上一次事故就是把它当成了道纹名。
    """
    text = (ROOT / "副本" / "罪孽都市.md").read_text(encoding="utf-8")
    loop = next(line for line in text.splitlines() if "⇄" in line)
    assert "点金" in loop, f"罪孽闭环应以【点金】起讫：{loop}"
    assert "失忆X⇄" not in loop, f"【失忆】不是道纹名，不得出现在闭环串里：{loop}"
    assert "失忆" not in set(REGION_EXCLUSIVE_DAOWEN["罪孽都市"])


def test_dianjin_is_registered_with_its_amnesia_cost():
    """边界：引擎侧【点金】的代价类型是【失忆】、代价量 X、收益 80X 碎片。"""
    from engine.daowen import DaoWenEngine

    DaoWenEngine.register_all()
    assert "点金" in DaoWenEngine._registry
    assert "失忆" not in DaoWenEngine._registry, "【失忆】是代价类型，不得注册成道纹"
    calc = DaoWenEngine.resolve("点金", 2, caster=None)
    assert calc["cost_type"] == "失忆" and calc["cost"] == 2
    assert calc["forget_daowen"] == 2 and calc["shard_gain"] == 160
