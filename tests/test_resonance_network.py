"""
pytest - 残韵闭环完整性（引擎 CLOSED_LOOPS 必须与规则正文声明一致）

背景：现行将原杀伐/切割两轨首尾接成一个14节点【杀伐闭环】；
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


def engine_edges() -> set:
    return {(s, t, d) for edges in R.CLOSED_LOOPS.values() for s, t, d in edges}


def engine_undirected_edges() -> set:
    """双向口径下的无向边：登记 (a,类型,b) 等价于 (b,类型,a)。"""
    return {tuple(sorted([(s, t), (d, t)])) for s, t, d in engine_edges()}


# ---------- 正常路径 ----------

def test_all_readme_monster_transforms_registered():
    """正常路径：规则正文声明的每一条怪物原始道纹转化都必须在引擎中登记。

    2026-10-07 用户令：转化途径改为双向，正文用 ⇄（历史写法 → 同样接受）。
    """
    txt = rules_text()
    spec = set()
    # 双向写法：全力X⇄（转换）借力X；历史单向写法：全力X→（转换）借力X
    for arrow in ("⇄", "→"):
        for m in re.finditer(r"(\w+?)X" + arrow + r"（(转换|反转|曲解)）(\w+?)X", txt):
            spec.add((m.group(1), m.group(2), m.group(3)))
        for m in re.finditer(
                r"(\w+?)X（代价[^）]*）" + arrow + r"（(转换|反转|曲解)）(\w+?)X", txt):
            spec.add((m.group(1), m.group(2), m.group(3)))
    assert spec, "未能从规则正文解析出怪物转化关系"
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
    """正常路径：原始怪物道纹 ⇄ 转化道纹 可反向（如 弱化→(反转)→全力）。

    2026-10-11 用户令删除【借力】→【全力】的（转换）出边**留空不补**
    （用户口径：整体大改前不新增道路）；【全力】仍有（反转）→弱化、（曲解）→再生。
    """
    assert R.find_transformation("弱化", "反转") == "全力"
    assert R.find_transformation("再生", "曲解", target_daowen="全力") == "全力"
    assert R.find_transformation("寄生", "曲解") is None  # 自愈删除后不补新路径
    assert R.get_available_resonance("全力") == [
        {"resonance_type": "反转", "target_daowen": "弱化", "direction": "正向", "loop": "怪物原始道纹"},
        {"resonance_type": "曲解", "target_daowen": "再生", "direction": "正向", "loop": "怪物原始道纹"},
    ]


def test_single_fourteen_node_core_loop():
    """正常路径：杀伐闭环为唯一11节点核心闭环（2026-08-21：冲击改名波及；删除缓慢、慈悲、切割）。"""
    assert "杀伐闭环" in R.CLOSED_LOOPS
    assert "切割闭环" not in R.CLOSED_LOOPS
    edges = R.CLOSED_LOOPS["杀伐闭环"]
    assert len(edges) == 11
    assert sorted(source for source, _, _ in edges) == sorted(target for _, _, target in edges)
    assert R.find_transformation("连击", "转换") == "波及"
    assert R.find_transformation("波及", "反转") == "增殖"
    assert R.find_transformation("封印", "反转") == "杀伐"
    for removed in ("冲击", "慈悲", "切割", "缓慢"):
        assert not any(src == removed or tgt == removed for src, _, tgt in edges)


def test_three_region_loops_present():
    """正常路径：三条副本闭环必须存在"""
    # 2026-10-03：扭曲都市删【爆裂】、龙心谷删【活血】，这两条闭环 8 → 7 条边；罪孽都市不变。
    # 2026-10-11：龙心谷删【嫁祸】（效果与遗物【贪婪契约】重复），由【裂变】直连【背负】，7 → 6 条边。
    # 2026-10-14：扭曲都市删【搏命】（并入【透支】），该格留成空占位符（镇尸先例），仍 7 条边。
    expected = {"扭曲都市闭环": 7, "罪孽都市闭环": 8, "龙心谷闭环": 6}
    for loop, n in expected.items():
        assert loop in R.CLOSED_LOOPS, f"缺少 {loop}"
        assert len(R.CLOSED_LOOPS[loop]) == n, f"{loop} 应有{n}条边"


def test_monster_daowen_now_transformable():
    """正常路径：怪物面板常见道纹必须有可用残韵路径"""
    # 2026-10-03：狂暴删除；飞行暂无残韵路径（原转换→滑翔、反转→坠落都随之删除，
    # 是否给飞行补新路径待用户裁定，见 报告.md）。
    for dw in ("必中", "全力", "疯狂", "减速"):
        paths = R.get_available_resonance(dw)
        assert paths, f"{dw} 仍无残韵路径"


# ---------- 边界条件 ----------

def test_region_loops_are_closed():
    """边界：三条副本闭环必须真正合拢（每个节点入度=出度=1）"""
    for loop in ("扭曲都市闭环", "罪孽都市闭环", "龙心谷闭环"):
        edges = R.CLOSED_LOOPS[loop]
        srcs = [s for s, _, _ in edges]
        dsts = [d for _, _, d in edges]
        assert sorted(srcs) == sorted(dsts), f"{loop} 未合拢：{sorted(set(srcs) ^ set(dsts))}"


def test_no_duplicate_declared_edge():
    """边界：登记的**正向**边里，同一 (源道纹, 残韵类型) 仍不得重复指向两个结果"""
    seen = {}
    for s, t, d in engine_edges():
        key = (s, t)
        assert key not in seen or seen[key] == d, f"{s}+{t} 同时指向 {seen[key]} 与 {d}"
        seen[key] = d



def test_ambiguous_path_requires_explicit_target():
    """仅在至少两个真实道纹出口仍存在时要求显式选择，不把占位符当候选。"""
    ambiguous = {(s, t) for s, t, _ in engine_edges() if R.is_ambiguous_path(s, t)}
    checked = 0
    for s, t in sorted(ambiguous):
        candidates = [c["target_daowen"] for c in R.find_transformations(s, t)
                      if not c.get("reserved")]
        if len(candidates) < 2:
            continue
        checked += 1
        assert R.find_transformation(s, t) is None
        res = R.apply_resonance(s, t, True, True, resonance_stock={t: 1})
        assert res["success"] is False and res.get("ambiguous") is True, (s, t, res)
        for dst in candidates:
            assert R.find_transformation(s, t, dst) == dst
            ok = R.apply_resonance(s, t, True, True, resonance_stock={t: 1}, target_daowen=dst)
            assert ok["success"] is True and ok["target"] == dst, (s, t, dst, ok)
        bad = R.apply_resonance(s, t, True, True, resonance_stock={t: 1}, target_daowen="不存在的道纹")
        assert bad["success"] is False
    assert checked > 0, "仍应存在至少一个具有多个真实出口的歧义路径"



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
