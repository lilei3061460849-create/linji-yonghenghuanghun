"""
pytest - 残韵闭环完整性（引擎 CLOSED_LOOPS 必须与规则正文声明一致）

背景：现行杀伐闭环为10个节点；原杀伐/切割两轨已合并并持续去重。
规则正文声明的三条副本闭环与怪物原始道纹转化也必须完整登记，
导致对怪物面板道纹发动残韵必然失败。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.daowen import ResonanceEngine as R, is_reserved_slot, display_resonance_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rules_text() -> str:
    """规则正文事实源：规则正文.md#第四宇宙规则正文（见 AI_EXPERIENCE.md《文档分工与事实源》）。"""
    with open(os.path.join(ROOT, "规则正文.md"), encoding="utf-8") as f:
        return f.read()


def tetrahedron_spec(text: str | None = None) -> set:
    """从规则正文的四面体串解析出 (节点, 残韵, 节点) 三元组。

    关系表改为四面体，正文写法为
    「反转 A⇄B、C⇄D ｜ 转换 A⇄C、B⇄D ｜ 曲解 A⇄D、B⇄C」。
    """
    txt = text if text is not None else rules_text()
    spec = set()
    for line in txt.splitlines():
        if "⇄" not in line:
            continue
        for seg in re.split(r"[｜|]", line):
            m = re.search(r"(转换|反转|曲解)[：:]?\s+(.*)", seg)
            if not m:
                continue
            rtype = m.group(1)
            for left, right in re.findall(
                    r"([\u4e00-\u9fff·]+?)⇄([\u4e00-\u9fff·]+?)(?=[、，,；;]|\s|$)", m.group(2)):
                spec.add((left, rtype, right))
    return spec


def engine_edges() -> set:
    return {(s, t, d) for edges in R.CLOSED_LOOPS.values() for s, t, d in edges}


def engine_undirected_edges() -> set:
    """双向口径下的无向边：登记 (a,类型,b) 等价于 (b,类型,a)。"""
    return {tuple(sorted([(s, t), (d, t)])) for s, t, d in engine_edges()}


# ---------- 正常路径 ----------

def test_all_readme_monster_transforms_registered():
    """正常路径：规则正文声明的每一条怪物原始道纹转化都必须在引擎中登记。

    转化途径改为双向，正文用 ⇄（历史写法 → 同样接受）。
    """
    txt = rules_text()
    spec = tetrahedron_spec(txt)
    assert spec, "未能从规则正文解析出四面体关系"
    missing = [e for e in spec
               if tuple(sorted([(e[0], e[1]), (e[2], e[1])])) not in engine_undirected_edges()]
    assert not missing, f"引擎缺失规则正文声明的转化：{sorted(missing)}"



def test_transform_paths_are_bidirectional():
    """真实道纹路径双向可达；遗物化节点只保留闭环边，不可获得。"""
    for s, t, d in engine_edges():
        assert any(p["target_daowen"] == display_resonance_node(d) and p["resonance_type"] == t
                   for p in R.get_available_resonance(s))
        assert any(p["target_daowen"] == display_resonance_node(s) and p["resonance_type"] == t
                   for p in R.get_available_resonance(d))
        if is_reserved_slot(d):
            assert is_reserved_slot(R.find_transformation(s, t, display_resonance_node(d)))
        else:
            assert R.find_transformation(s, t, d) == d, f"{s}--{t}-->{d} 正向不可达"
        if is_reserved_slot(s):
            assert is_reserved_slot(R.find_transformation(d, t, display_resonance_node(s)))
        else:
            assert R.find_transformation(d, t, s) == s, f"{d}--{t}-->{s} 反向不可达"


def test_monster_transform_branch_is_reversible():
    """正常路径：原始怪物道纹与转化道纹同处通用四面体三，路径双向。

    2026-11-02 四面体三：反转 强化⇄弱化、全力⇄全速 ｜ 转换 强化⇄全力、弱化⇄全速 ｜
    曲解 强化⇄全速、弱化⇄全力。每个节点在每种残韵上只有一个出口，不再需要显式选目标。
    """
    assert R.find_transformation("弱化", "反转") == "强化"
    assert R.find_transformation("全力", "反转") == "全速"
    assert R.find_transformation("强化", "转换") == "全力"
    assert R.find_transformation("弱化", "转换") == "全速"
    assert R.find_transformation("弱化", "曲解") == "全力"
    assert R.find_transformation("强化", "曲解") == "全速"
    assert R.find_transformation("寄生", "曲解") is None  # 自愈删除后不补新路径
    assert R.get_available_resonance("全力") == [
        {"resonance_type": "反转", "target_daowen": "全速", "direction": "正向", "loop": "通用四面体三"},
        {"resonance_type": "转换", "target_daowen": "强化", "direction": "反向", "loop": "通用四面体三"},
        {"resonance_type": "曲解", "target_daowen": "弱化", "direction": "反向", "loop": "通用四面体三"},
    ]


def test_generic_core_is_four_tetrahedra():
    """正常路径：通用核心由四面体一~四构成，16 节点 96 个有向关系。

    环状链 + 占位节点整体改为四面体；【操纵】由【操作】改名，
    作用次数改为「下X次」。
    """
    assert "切割闭环" not in R.CLOSED_LOOPS
    generic = R.tetrahedra_of_region("通用")
    assert set(generic) == {"通用四面体一", "通用四面体二", "通用四面体三", "通用四面体四"}
    assert len(R.edges_of_region("通用")) == 24  # 4 个四面体 × 6 条无向边
    assert set(R.nodes_of_region("通用")) == {
        "杀伐", "再生", "庇护", "脆弱",
        "操纵", "束缚", "迟滞", "篡改",
        "强化", "弱化", "全力", "全速",
        "必中", "飞行", "压迫", "封印",
    }
    assert R.find_transformation("脆弱", "反转") == "庇护"
    assert R.find_transformation("操纵", "转换") == "迟滞"
    # 【篡改】与【封印】再次调换（封印回四面体二、篡改回四面体四）。
    assert R.find_transformation("迟滞", "反转") == "封印"
    assert R.find_transformation("杀伐", "曲解") == "庇护"
    assert R.find_transformation("束缚", "曲解") == "迟滞"
    # 扭曲都市的两组节点不再同环：超频在四面体一、增殖在四面体二。
    assert R.find_transformation("超频", "反转") == "萎缩"
    assert R.find_transformation("增殖", "反转") == "饥饿"
    assert R.find_transformation("波及", "反转") == "流动"
    assert R.find_transformation("封印", "反转") == "迟滞"
    assert R.find_transformation("篡改", "反转") == "压迫"
    for removed in ("冲击", "慈悲", "切割", "缓慢"):
        assert removed not in R.nodes_of_region("通用")


def test_sunken_sea_former_draft_nodes_can_be_acquired_by_resonance():
    from engine.gamedata import UNIMPLEMENTED_DAOWEN
    for source, rtype, target in (
        ("回响", "转换", "波及"),
        ("波及", "反转", "流动"),
        ("大梦", "曲解", "冰冻"),
    ):
        assert target not in UNIMPLEMENTED_DAOWEN
        result = R.apply_resonance(source, rtype, caster_has_daowen=True,
                                   target_has_daowen=True, target_daowen=target)
        assert result["success"] is True and result["target"] == target


def test_race_exclusive_sunken_sea_nodes_cannot_be_acquired_by_resonance():
    for source, rtype, target in (
        ("遗忘", "曲解", "唤潮"),
        ("大梦", "曲解", "蜃景"),
    ):
        result = R.apply_resonance(source, rtype, caster_has_daowen=True,
                                   target_has_daowen=True, target_daowen=target)
        assert not result["success"]
        assert "不通向" in result["error"]


def test_region_tetrahedra_present():
    """正常路径：每个副本的四面体都在，且各自 4 节点 6 边。"""
    # 2026-11-08：龙心谷/乱葬岗/荒疫古城/永夜庭各补齐第二组四面体（满 8 纹）。
    expected = {"扭曲都市": 2, "罪孽都市": 2, "龙心谷": 2, "乱葬岗": 2,
                "沉沦海": 2, "荒疫古城": 2, "永夜庭": 2}
    for region, count in expected.items():
        tetras = R.tetrahedra_of_region(region)
        assert len(tetras) == count, f"{region} 应有{count}个四面体，实：{list(tetras)}"
        for name, edges in tetras.items():
            assert len(edges) == 6, f"{name} 边数应为6"
            assert len({n for s, _, d in edges for n in (s, d)}) == 4, f"{name} 节点数应为4"


def test_monster_daowen_now_transformable():
    """正常路径：仍在册的原始怪物道纹必须有可用残韵路径。

    【疯狂】【减速】遗物化退出道纹体系，不再有残韵路径；
    【狂暴】2026-10-03 删除、【飞行】是状态。现存原始道纹只剩【全力】【必中】。
    """
    for dw in ("必中", "全力"):
        paths = R.get_available_resonance(dw)
        assert paths, f"{dw} 仍无残韵路径"


# ---------- 边界条件 ----------

def test_disaster_ward_is_removed_from_sin_city_loop():
    edges = R.edges_of_region("罪孽都市")
    assert len(edges) == 12  # 两个四面体 × 6 边
    assert not any(src == "消灾" or dst == "消灾" for src, _, dst in edges)
    assert R.get_available_resonance("消灾") == []
    assert R.find_transformation("假钞", "曲解") == "点金"
    assert R.find_transformation("交易", "转换") == "赦免"


def test_region_tetrahedra_are_complete_graphs():
    """边界：每个四面体内 4 个节点两两相连、恰好各一条边。"""
    for region in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗",
                   "沉沦海", "荒疫古城", "永夜庭"):
        for name, edges in R.tetrahedra_of_region(region).items():
            nodes = sorted({n for s, _, d in edges for n in (s, d)})
            pairs = {frozenset((s, d)) for s, _, d in edges}
            assert len(pairs) == 6, f"{name} 有重复的节点对"
            assert all(frozenset((a, b)) in pairs
                       for i, a in enumerate(nodes) for b in nodes[i + 1:]), \
                f"{name} 不是完全图"


def test_no_duplicate_declared_edge():
    """同源同残韵可保留多个显式分叉，但不得由引擎静默任选其一。"""
    outgoing = {}
    for source, resonance_type, target in engine_edges():
        outgoing.setdefault((source, resonance_type), set()).add(target)
    for (source, resonance_type), targets in outgoing.items():
        if len(targets) < 2:
            continue
        assert R.find_transformation(source, resonance_type) is None
        for target in targets:
            assert R.find_transformation(
                source, resonance_type, target_daowen=target) == target



def test_tetrahedra_have_no_ambiguous_single_outlet():
    """边界：四面体拓扑下每个（源, 残韵）至多一个真实出口，不再需要显式选目标。

    2026-11-02 之前是环状链 + 分叉，同一源同一残韵会有两个出口；改四面体后
    每条边的两端都唯一，`find_transformation` 不再返回 None，也不该出现歧义。
    """
    for s, t, d in engine_edges():
        assert R.is_ambiguous_path(s, t) is False, f"{s}--{t}-->{d} 仍是歧义路径"
        assert R.find_transformation(s, t) is not None
    res = R.apply_resonance("杀伐", "反转", True, True, resonance_stock={"反转": 1})
    assert res["success"] is True and res["target"] == "再生"
    bad = R.apply_resonance("杀伐", "反转", True, True, resonance_stock={"反转": 1},
                            target_daowen="不存在的道纹")
    assert bad["success"] is False



def test_find_transformation_roundtrip():
    """真实道纹可双向查找；遗物化占位符不作为可获得结果。"""
    for s, t, d in engine_edges():
        if is_reserved_slot(d):
            assert is_reserved_slot(R.find_transformation(s, t, display_resonance_node(d)))
        else:
            assert R.find_transformation(s, t, d) == d
        if is_reserved_slot(s):
            assert is_reserved_slot(R.find_transformation(d, t, display_resonance_node(s)))
        else:
            assert R.find_transformation(d, t, s) == s


def test_unknown_daowen_has_no_path():
    """错误输入：不存在的道纹不得返回任何路径"""
    assert R.get_available_resonance("不存在的道纹") == []
    assert R.find_transformation("不存在的道纹", "反转") is None


def test_invalid_resonance_type_returns_none():
    """错误输入：非法残韵类型必须返回 None，不得静默命中"""
    assert R.find_transformation("杀伐", "乱写") is None
    assert R.find_transformation("杀伐", "") is None
