"""核心规则压缩批：data/rules/game_rules.toml ↔ 规则正文 ↔ 《死者之书》规则篇 三方一致。

这是「规则压进死者之书」闭环的守护测试。要证明的闭环：

    data/rules/game_rules.toml ──► 《死者之书.md》「## 规则」节「核心规则」章
                                 （rule_lines 压缩讲述，署名 ？？？）
    规则正文.md「核心规则」节 ──► precise_lines 逐行照录（断言与 规则正文 逐字一致，
                                 压缩不得悄悄漂移完整正文）
    tests 钉住：逐条从第一条规则往后压（order 连续不许跳号）、压缩不得比原文长、
    书页不得长出「道纹X：…」正文匹配（会污染 rule_sync 的事实源道纹计数）。

压缩口径（防误解，违反即挂测试或人工审查不过）：只压缩，不造假——规则节
「所有人看到的都一样」，压缩讲述不得与引擎结算矛盾；误导留给署人名的遗言层。

新增/压缩一条规则的正确姿势：在 toml 追加 [[rule]]（precise_lines 照录 规则正文
原文 + rule_lines 写压缩讲述）→ 跑 `python3 sim/gen_rules.py` → 跑本文件。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.rule_sync import RuleSync  # noqa: E402
from engine.rules_source import game_rules, lethal_events  # noqa: E402

BOOK = ROOT / "死者之书.md"
RULES_TEXT = ROOT / "规则正文.md"


def test_every_core_rule_has_a_signed_page_in_the_book():
    """每条核心规则在书里都有一页，署名 ？？？，压缩讲述逐行出现在页里。"""
    text = BOOK.read_text(encoding="utf-8")
    for entry in game_rules.all():
        heading = f"### {game_rules.signature}·{game_rules.chapter}·{entry['name']}"
        assert heading in text, f"死者之书缺少规则页：{heading}"
        page = text.split(heading, 1)[1].split("\n### ", 1)[0]
        for rule_line in entry["rule_lines"]:
            assert rule_line in page, f"{entry['name']} 页缺少压缩讲述：{rule_line[:60]}"


def test_precise_lines_match_rules_text_verbatim():
    """precise_lines 必须是 规则正文 原文的逐行照录——完整正文以 规则正文 为准。"""
    rules_lines = RULES_TEXT.read_text(encoding="utf-8").splitlines()
    for entry in game_rules.all():
        assert entry["precise_lines"], f"{entry['name']} 缺 precise_lines（必须照录原文）"
        for line in entry["precise_lines"]:
            assert line in rules_lines, (
                f"{entry['name']} 的 precise_line 不是 规则正文 原文行：{line[:60]}")


def test_rules_are_ported_in_order_and_actually_compressed():
    """逐条从第一条规则往后压：order 连续从 1 开始；压缩不得比原文长。"""
    entries = game_rules.all()
    assert entries, "game_rules.toml 还没有任何规则"
    assert [e["order"] for e in entries] == list(range(1, len(entries) + 1)), \
        "核心规则必须从第一条开始逐条压，order 连续不许跳号"
    for entry in entries:
        precise_len = sum(len(line) for line in entry["precise_lines"])
        compressed_len = sum(len(line) for line in entry["rule_lines"])
        assert entry["rule_lines"], f"{entry['name']} 的 rule_lines 为空"
        assert compressed_len < precise_len, \
            f"{entry['name']} 压缩后反而更长，不叫压缩：{compressed_len} >= {precise_len}"


def test_book_rule_pages_do_not_pollute_daowen_extraction():
    """规则页的压缩讲述不得长出「道纹X：…」正文匹配——会污染事实源道纹计数。

    规则正文提取（rule_sync.extract_daowen_from_file）按行扫「某某X：…」与
    「某某X（消耗…）」模式；死者之书至今贡献 0 条，压缩讲述必须保持 0 条。
    """
    sync = RuleSync(db_path="data/test_rule_sources.db")
    extracted = sync.extract_daowen_from_file("死者之书.md")
    assert extracted == [], f"死者之书规则页污染了道纹提取：{extracted}"


def test_all_rule_files_share_the_question_mark_signature():
    """所有规则事实源的署名必须一致——规则不署人名，署人名的是遗言。"""
    assert game_rules.signature == lethal_events.signature == "？？？"
