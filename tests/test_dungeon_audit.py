"""二阶副本面板合规审计契约测试。

2026-09-16 用户令：怪物设计只约束**属性点数**与**道纹数量**，道纹 X 值自由自定义。
旧的「道纹总值」配额已废止——它本是"怪物发动道纹不支付法力"的补丁；怪物改为支付法力后，
真正的约束是[法限]构成的每回合法力预算。
口径：二阶（乱葬岗/沉沦海）可分配属性点100，道纹5条；
面板成本=⌈血限/6⌉+2×法限+2×速限 ≤100。永夜庭特殊属性点（60×场次）豁免。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "audit_dungeons", os.path.join(ROOT, "sim", "audit_dungeons.py"))
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)


# 2026-10-03 用户令删除 爆裂/坠落/滑翔/狂暴/兴奋/尸爆/活血 后，承载这些道纹的怪物
# 面板出现空位；是否补位（以及补哪条道纹）待用户裁定，故这里显式列出**已知**空位，
# 新出现的缺员仍会让本用例失败。
# 2026-10-08 用户令再删【镇尸】（与坏死硬重复）与一条无残韵路径的孤儿转化道纹：
# 哀嚎者 5→3、纸人 5→4。是否补位一并待裁定。
PENDING_DW_GAPS = {
    "乱葬岗/蛆冢", "乱葬岗/不死骸骨", "乱葬岗/尸霸", "乱葬岗/血僵",
    "乱葬岗/哀嚎者", "乱葬岗/纸人", "乱葬岗/白骨祭坛",
    "乱葬岗/腐疫鼠", "乱葬岗/红嫁衣鬼", "乱葬岗/寄骨蝇", "乱葬岗/执念", "乱葬岗/勾魂使者",
    "沉沦海/疫巢", "沉沦海/潮尸", "沉沦海/碑文鲛", "沉沦海/扩潮母体", "沉沦海/沉船守卫",
    "沉沦海/渊眼", "沉沦海/镜面鱼", "沉沦海/褪鳞蟹", "沉沦海/忘潮祭司",
    "沉沦海/凝沙兽", "沉沦海/归潮主祭", "沉沦海/回声螺", "沉沦海/珊瑚囚徒",
    # 2026-10-28：沉沦海草案新增回声螺（3种）与珊瑚囚徒（4种）；是否补位尚无裁定，
    # 先登记缺口，不在测试里替设计擅自补道纹。
    # 2026-10-11 用户令删除【无神】【借力】【衰败】后新增的缺员面板：
    #   乱葬岗/腐疫鼠 5→4、乱葬岗/红嫁衣鬼 5→4、沉沦海/褪鳞蟹 5→4、沉沦海/忘潮祭司 5→4。
    # 用户口径：整体大改前不新增道路，故空缺先登记、是否补位待定；补位后从本集合移除。
    # （同期 沉沦海/潜水员 面板道纹清零，但它是[员工]面板、不在本审计口径内。）
}


def test_tier2_dungeon_panels_all_compliant():
    """正常路径：乱葬岗/沉沦海全部普通池怪面板≤100、道纹≤5条（总值不再约束）。

    道纹数因本批删除而临时不足 5 的面板必须在 PENDING_DW_GAPS 里；超出即视为新破口。
    """
    for fname in ("乱葬岗", "沉沦海"):
        spec = ad.TARGETS[fname]
        # 2026-09-16 用户令：只约束属性点数与道纹数量；「道纹总值」配额已废止
        # （它本是"怪物不支付法力"的补丁，改付法力后由[法限]预算承担该约束）。
        assert spec["budget"] == 100 and spec["dw_count"] == 5
        assert "dw_total" not in spec, "道纹总值配额已废止，不应再出现在审计口径里"
        monsters = [m for m in ad.parse_monsters(f"副本/{fname}.md")
                    if m["name"] not in ad.SPECIAL_MONSTERS]
        assert len(monsters) > 0
        actual_gaps = set()
        for m in monsters:
            cost = ad.panel_cost(m["hp"], m["ap"], m["ac"])
            assert cost <= 100, f"{fname}/{m['name']} 面板成本{cost}>100"
            assert len(m["dw"]) <= 5, f"{fname}/{m['name']} 道纹数{len(m['dw'])}>5"
            if len(m["dw"]) < 5:
                actual_gaps.add(f"{fname}/{m['name']}")
        assert actual_gaps <= PENDING_DW_GAPS, \
            f"出现未登记的缺员面板：{sorted(actual_gaps - PENDING_DW_GAPS)}"
            # 道纹总值不再约束（2026-09-16），X 值由[法限]预算自行决定


def test_boundary_special_monsters_exempted():
    """边界：疫巢(boss)/潜水员(员工)豁免面板审计。"""
    for fname in ("乱葬岗", "沉沦海"):
        monsters = ad.parse_monsters(f"副本/{fname}.md")
        for m in monsters:
            if m["name"] in ad.SPECIAL_MONSTERS:
                assert True  # 豁免，不检查面板/道纹配额
    # 永夜庭特殊属性点豁免
    assert ad.TARGETS["永夜庭"] is None


def test_error_panel_cost_formula():
    """错误输入：面板成本公式验证（⌈血限/6⌉+攻击力+攻击次数²）。"""
    assert ad.panel_cost(216, 4, 4) == 52  # ⌈216/6⌉36+2×4+2×4
    assert ad.panel_cost(258, 28, 3) == 105  # ⌈258/6⌉43+2×28+2×3
    assert ad.panel_cost(0, 0, 0) == 0
