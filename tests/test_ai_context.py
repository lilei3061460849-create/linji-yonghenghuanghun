"""外部 AI 单文件入口：规范源 → AI_CONTEXT.md 的可复现、剧情优先聚合。

外部 AI 常只会读取一个 GitHub raw URL，不能保证继续抓 README 的相对链接。AI_CONTEXT.md
优先保留世界观、角色名单、角色扮演约束与外部推演守则，并附机制速查：道纹／遗物／
法术／已实现副本各取名称与一行效果（2026-10-14 用户令——外部 AI 不知道具体道纹与
遗物就会胡说八道）。通用规则正文、完整推演规范、副本机制细节与战报仍按需查询，
不成为新的规则正本。

本文件守住：
- 生成物逐字等于生成器输出；
- 来源＝故事正典＋AI 知识库扮演规范＋四张机制速查表＋角色名单；
- 通用规则正文、完整推演规范与副本机制不批量嵌入；
- 外部推演守则（单角色／选项制）确实被打包；
- README 保留外部 raw 入口，且说明生成物不是事实源。
"""
from pathlib import Path

from engine.document_sources import AI_CONTEXT_FILE, README_FILE
from sim.gen_ai_context import (
    OUTPUT,
    context_blocks,
    context_sources,
    digest_sources,
    render_context,
)

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / README_FILE

REQUIRED_SOURCES = {"故事文档.md", "AI_EXPERIENCE.md"}
# 2026-10-24 用户令：AI 上下文必须能回答「这一手怎么打」。
# 规则正文只以「战斗行动」节的整段摘录进入（见 extract_actions），
# 其余机制原文照旧按需查询，因此它从 EXCLUDED_MECHANICS_SOURCES 移入速查。
DIGEST_SOURCES = {"规则正文.md", "全道纹索引.md", "物品索引.md", "法术索引.md", "副本索引.md", "角色记忆"}
EXCLUDED_MECHANICS_SOURCES = {
    "推演规范.md", "死者之书.md",
    "副本/扭曲都市.md", "副本/罪孽都市.md", "副本/龙心谷.md", "副本/乱葬岗.md",
}

FORBIDDEN_IN_BODY = (
    "```toml", "data/rules", "gen_rules", "gen_ai_context", "sim/", "engine/", "tests/",
    "action_count", "define_spell", "use_daowen",
    "战斗推演格式", "报告书写口径", "遗言格式", "自创法术标准语法", "触发时机接线状态",
    "收录规则", "待核查区", "正文区", "未实现副本草案遗物", "鲛族遗物沉沦海草案",
    "数值规则迁移状态", "新增副本规范", "未实现规则草案",
    "草案", "未实现", "占位", "待补", "尚未接入", "未接入",
)


def _bodies() -> dict[str, str]:
    return {relpath: body for _, relpath, body, _ in context_blocks()}


def test_ai_context_is_exactly_up_to_date_with_its_sources():
    """生成物逐字等于当前 render；防止忘记重生成。"""
    assert OUTPUT == ROOT / AI_CONTEXT_FILE
    assert OUTPUT.is_file(), "缺少外部 AI 单文件入口：AI_CONTEXT.md"
    assert OUTPUT.read_text(encoding="utf-8") == render_context(), (
        "AI_CONTEXT.md 与当前来源不一致，请跑 python3 sim/gen_ai_context.py")


def test_context_covers_story_sources_digests_and_excludes_bulk_mechanics():
    """来源＝故事正典＋扮演规范＋机制速查＋角色名单；机制原文与副本机制仍按需查询。"""
    paths = {source.relative_path for source in context_sources()}
    paths |= {digest.path for digest in digest_sources()}
    assert paths == REQUIRED_SOURCES | DIGEST_SOURCES
    assert not (paths & EXCLUDED_MECHANICS_SOURCES)
    assert not any(path.endswith(".toml") for path in paths)
    assert README_FILE not in paths
    assert "报告.md" not in paths

    text = OUTPUT.read_text(encoding="utf-8")
    for relpath, body in _bodies().items():
        assert f"- 路径：`{relpath}`" in text
        assert body in text, f"AI_CONTEXT 缺少过滤后的来源正文：{relpath}"


def test_action_digest_is_only_the_combat_action_section():
    """边界：规则正文进上下文的那一块只能是「战斗行动」整段，不许夹带别的规则章节。"""
    body = _bodies()["规则正文.md"]
    assert "攻击（占1次主动出手）" in body
    assert "招架（回合外，不占出手，仅对攻击）" in body
    # 只摘一节：篇幅有界，且其余章节标题一个都不许出现
    assert len(body) < 3000, f"行动摘录过长，疑似整篇规则被塞进来：{len(body)} 字"
    for other in ("基础定义", "道纹体系", "核心规则", "怪物准则", "特殊事件", "通用事件池"):
        assert other not in body, f"行动摘录混入了《规则正文》其它章节：{other}"


def test_context_body_contains_no_code_interface_format_or_future_plans():
    """收录正文不含代码、接口、机械格式与未来规划。"""
    for path, body in _bodies().items():
        assert "`" not in body, f"正文残留代码反引号：{path}"
        for marker in FORBIDDEN_IN_BODY:
            assert marker not in body, f"正文残留「{marker}」：{path}"


def test_mechanics_digest_covers_daowen_relics_spells_and_regions():
    """机制速查必须给出具体名称与一行效果，供外部 AI 避免凭空编造。"""
    bodies = _bodies()
    daowen, relics = bodies["全道纹索引.md"], bodies["物品索引.md"]
    spells, regions = bodies["法术索引.md"], bodies["副本索引.md"]

    for marker in ("杀伐：支付：法力X。", "封印：支付：异变X", "透支：支付：", "变形：支付：法力X。"):
        assert marker in daowen, marker
    for name in ("不死", "出土", "篡改", "催化", "净化"):
        assert f"{name}：" in daowen, "已实装效果不能因所属副本尚未开放而被摘要丢弃"
    for name in ("流动", "遗忘"):
        assert f"{name}：" not in daowen, "未实装现行定义不能被摘要当作可执行规则"
    assert "分裂：支付：流血X+Y，创造一个X攻击力/Y血限的造物。" in daowen

    for marker in ("守夜灯：", "万钧印：", "回锋刀：", "痛苦的偏爱：", "猩红果实："):
        assert marker in relics, marker
    assert "???" not in relics and "占位" not in relics

    for marker in ("先发制人：所需道纹 杀伐", "镇魔印：所需道纹 封印", "血炼周天："):
        assert marker in spells, marker

    for marker in ("扭曲都市（一阶", "乱葬岗（二阶"):
        assert marker in regions, marker


def test_external_roleplay_rules_are_packed_for_outside_ai():
    """外部推演守则（单角色·选项制）必须进入单文件上下文。"""
    text = OUTPUT.read_text(encoding="utf-8")
    for marker in (
        "### 外部推演守则",
        "只扮演当前角色",
        "不给其他角色加戏",
        "类海龟汤",
        "战斗／逃跑／自定义",
    ):
        assert marker in text, marker


def test_readme_exposes_the_one_url_external_ai_entry_and_context_contract():
    """README 暴露 raw 入口；上下文说明剧情优先、事实源边界与按需查询。"""
    readme = README.read_text(encoding="utf-8")
    assert "[AI_CONTEXT.md](AI_CONTEXT.md)" in readme
    assert ("https://raw.githubusercontent.com/lilei3061460849-create/"
            "linji-yonghenghuanghun/main/AI_CONTEXT.md") in readme

    text = OUTPUT.read_text(encoding="utf-8")
    for marker in (
        "## 使用契约（先读）", "## 来源目录与校验指纹", "## 打包正文",
        "死者之书的遗言不是规则", "副本状态要区分", "禁止手改",
        "剧情优先的混合上下文", "按需查询", "机制速查",
    ):
        assert marker in text
    assert len(text) < 25000, "AI_CONTEXT 应明显小于原先包含完整机制索引的版本"


def test_context_cli_writes_and_checks_the_generated_output(tmp_path, monkeypatch):
    """CLI 必须真正输出文件；--check 应发现过期内容且不能改写文件。"""
    from sim import gen_ai_context

    output = tmp_path / "AI_CONTEXT.md"
    monkeypatch.setattr(gen_ai_context, "OUTPUT", output)
    assert gen_ai_context.main(["--check"]) == 1
    assert not output.exists()
    assert gen_ai_context.main([]) == 0
    assert output.read_text(encoding="utf-8") == gen_ai_context.render_context()
    assert gen_ai_context.main(["--check"]) == 0
    output.write_text("stale\n", encoding="utf-8")
    assert gen_ai_context.main(["--check"]) == 1
    assert output.read_text(encoding="utf-8") == "stale\n"
