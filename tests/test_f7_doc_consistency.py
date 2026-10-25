"""
F7 验证：文档一致性（全程自动触发 + 命名漂移 + engine/README 复审）
- 正常：推演规范「五、全程自动触发」列表与规则正文特殊事件节 14 项对齐
- 边界：活跃代码层（engine/*.py, sim/*.py, tests/*.py）不再出现中文“增生”，仅允许在废弃别名注释中出现
- 边界（2026-10-08）：规则正文与引擎文案不再出现“迷失”，已统一为“迷失”
- 错误：对旧名“增生”的显式调用应被拒绝或不存在
- 引擎规则正文：文件结构已补全至当前19项且含F7订正注记
"""
import pathlib
import re

def test_normal_auto_trigger_list_matches_rules_text():
    rules = pathlib.Path("规则正文.md").read_text(encoding="utf-8")
    playbook = pathlib.Path("推演规范.md").read_text(encoding="utf-8")
    # 精确规则事实源在规则正文；流程的「五、全程自动触发」清单在推演规范。
    m = re.search(r"五、全程自动触发[^\n]*\n([^\n]+)", playbook)
    assert m, "未找到 五、全程自动触发 段"
    line = m.group(1)
    # 必须包含 凡庸 / 癌变 / 迷失（本次 F7 补漏的三项；2026-10-08 迷失统一为迷失）
    for kw in ["凡庸", "癌变", "迷失"]:
        assert kw in line, f"五章列表应包含 {kw}，实际为：{line}"
    # 2026-10-24 起【背叛】不再是特殊事件、也不自动触发；2026-10-25 起它连独立条目都不留，
    # 改名【夺书】并退回「视野内目标，自由选择是否战斗」的通用机制，故不在清单里。
    expected_auto = ["死者之页","凡庸","癌变","还债","雕塑","许愿","逃跑与追击","进化","迷失","撤退","最终的冠冕","初拥之夜","救赎"]
    for kw in expected_auto:
        assert kw in line, f"同步后五章应含 {kw}"
    assert "背叛" not in line, f"背叛已改为自选项，不应留在自动触发清单里：{line}"
    assert "夺书" not in line, f"夺书不是自动触发事件，不应留在清单里：{line}"
    assert "【夺书】不在其列" in playbook
    # 验证特殊事件节的标题与五章一致（不校验数量，仅校验关键词存在）
    # 2026-10-25：特殊事件正文只留在《死者之书》，规则正文只留指针
    book = pathlib.Path("死者之书.md").read_text(encoding="utf-8")
    assert "凡庸（任一角色连续五回合" in book
    assert "多个角色触发凡庸时，非轮回者优先触发" in book
    assert "初始道纹发现：候选〔甲、乙、丙〕→选择【所选】" in playbook
    assert "遗物发现：候选〔甲、乙、丙〕→选择【所选】" in playbook
    assert "禁止只写选择结果" in playbook
    assert "癌变（任一角色在本场战斗内累计受到回复" in book
    assert "累计回复属于局内减益追踪，[战终]清零" in book
    assert "迷失（非怪物角色【异变】达到" in book
    assert "怪物不会触发" in book
    assert "救赎（怪物当前生命≤其[血限]10%" in book
    assert "### 特殊事件" in rules and "《死者之书.md》" in rules.split("### 特殊事件", 1)[1]

def test_boundary_no_zengsheng_in_active_code():
    """边界：活跃代码层不再出现中文“增生”（旧名），已统一为癌变；增殖道纹不受影响"""
    active_roots = [pathlib.Path("engine"), pathlib.Path("sim")]
    hits = []
    for root in active_roots:
        for p in root.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            # 排除掉 已知的兼容别名注释中允许出现的一次（engine/models.py 保留旧字段名的注释里含“增生”二字用于说明）
            # 为严格，我们要求除了 models.py 的那一行兼容注释外，其余不应出现
            if "增生" in text:
                # 计算行号
                for i, line in enumerate(text.splitlines(), 1):
                    if "增生" in line and "增殖" not in line:
                        # 2026-10-03：通用遗物【增生药剂】是用户令新增的正当名字，
                        # 与旧机制名「增生」（已改叫癌变）无关——只放行这一处。
                        if "增生药剂" in line:
                            continue
                        # 允许 models.py 的那一行（包含“旧名 增生”）
                        if p.name == "models.py" and "旧名" in line and "增生" in line:
                            continue
                        # 允许 combat.py（或其分片 engine/combat_parts/）里标注了“旧名”的兼容注释
                        if ("combat.py" == p.name or "combat_parts" in p.parts) and "旧名" in line:
                            continue
                        hits.append(f"{p}:{i}:{line.strip()}")
    assert hits == [], f"活跃代码仍含增生（应已全改为癌变）：{hits[:5]}"
    # 增殖道纹必须仍在（二者无关）
    assert pathlib.Path("engine/daowen.py").read_text(encoding="utf-8").count("增殖") > 0
    assert pathlib.Path("engine/models.py").read_text(encoding="utf-8").count("增殖") > 0

def test_error_old_name_rejected():
    """错误：对旧字段/旧阈值的显式错误使用应不存在"""
    # 旧阈值名 PROLIFERATION_THRESHOLD 应仍可用作别名，但新名 CANCER_THRESHOLD 应存在
    from engine.combat import CombatEngine
    assert hasattr(CombatEngine, "PROLIFERATION_THRESHOLD")
    assert hasattr(CombatEngine, "CANCER_THRESHOLD")
    assert CombatEngine.CANCER_THRESHOLD == CombatEngine.PROLIFERATION_THRESHOLD
    # 旧实体字段 is_proliferated 仍兼容，但新别名 is_cancer 应可用
    from engine.models import Entity
    e = Entity(name="test", entity_type="怪物")
    e.is_proliferated = True
    assert e.is_cancer is True
    e.is_cancer = False
    assert e.is_proliferated is False
    # 非法：若外部试图用“增生”作为道纹名应不存在（道纹池无此名）
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    assert "增生" not in DaoWenEngine._registry
    assert "癌变" not in DaoWenEngine._registry  # 癌变是机制非道纹，不在道纹注册表
    assert "增殖" in DaoWenEngine._registry

def test_engine_readme_completeness():
    """engine/README 已全面复审：文件结构当前19项 + F7订正注记。"""
    text = pathlib.Path("engine/README.md").read_text(encoding="utf-8")
    # 文件结构应包含新增的 8 个缺漏文件
    for fname in ["gamedata.py", "events.py", "battle_report.py", "ai_player.py", "ai_tactics.py", "rule_sync.py", "dungeons.py", "document_validation.py", "validator.py"]:
        assert fname in text, f"engine/README 文件结构应包含 {fname}"
    # F7 订正注记
    assert "F7 订正" in text or "增生" in text or "癌变" in text
    # 增生→癌变 说明
    assert "癌变" in text
    assert "增殖" in text

def test_no_active_zengsheng_in_tests_except_allowed():
    """tests 目录除本文件的允许注释外，不应再以“增生”作为机制名出现"""
    hits = []
    for p in pathlib.Path("tests").rglob("*.py"):
        if p.name == "test_f7_doc_consistency.py":
            continue
        text = p.read_text(encoding="utf-8")
        # 2026-10-03：逐行判定——只放行遗物名【增生药剂】，其余「增生」仍视为漏改。
        for i, line in enumerate(text.splitlines(), 1):
            if "增生" in line and "增生药剂" not in line:
                hits.append(f"{p}:{i}:{line.strip()}")
    # 允许 test_engine.py 中仍保留的 proliferation 英文 type 字符串，但中文不应再出现
    # 实际本轮已将 tests/test_engine.py 的增生中文改为癌变，故此处应为空
    # 若仍有，说明还有漏改
    assert hits == [], f"tests 仍含增生中文：{hits}"


def test_boundary_bengjie_term_retired_in_favour_of_mishi():
    """边界：规则正文与引擎文案里旧称已退场，统一为“迷失”（2026-10-08 用户令）。

    历史存档（data/、reports/、archive/）里的旧称指同一件事，不在扫描范围内。
    旧称二字在下方拼接成串，避免本文件被同类批量改名再次误伤。
    """
    retired = "崩" + "解"
    for rel in ("规则正文.md", "推演规范.md", "死者之书.md"):
        text = pathlib.Path(rel).read_text(encoding="utf-8")
        assert retired not in text, f"{rel} 仍出现旧称“{retired}”，应统一为“迷失”"
    # 2026-10-25：特殊事件正文只留在《死者之书》，规则正文只留指针，故不再在规则正文里找「迷失」
    assert "### ？？？·特殊事件·迷失" in pathlib.Path("死者之书.md").read_text(encoding="utf-8")
    hits = []
    for root in (pathlib.Path("engine"),):
        for p in root.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            if retired in p.read_text(encoding="utf-8"):
                hits.append(str(p))
    assert not hits, f"引擎仍出现旧称“{retired}”：{hits}"
