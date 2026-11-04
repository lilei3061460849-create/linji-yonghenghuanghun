"""
F8 验证：独立模拟器 simulate.py 已删除（旧口径，冻结于 PR#8 前）
- 正常：文件不存在，sim/ 为唯一事实源
- 边界：尝试导入 simulate 应失败
- 错误：外部若仍引用 simulate.py 路径应被拒绝/不存在
"""
import pathlib
import importlib.util
import pytest

def test_normal_simulate_deleted():
    """正常：simulate.py 已不存在"""
    assert not pathlib.Path("simulate.py").exists(), "simulate.py 应已删除"
    # sim/ 仍为事实源
    assert pathlib.Path("sim/balance_sim.py").exists()
    assert pathlib.Path("sim/audits/run_sim.py").exists()
    # 新战报生成器仍在
    assert pathlib.Path("sim/pick_best_report.py").exists()

def test_boundary_import_fails():
    """边界：尝试导入 simulate 应失败（文件不存在）"""
    spec = importlib.util.find_spec("simulate")
    # find_spec 对无文件模块应返回 None
    assert spec is None, "simulate 模块不应再可导入"
    # 直接尝试加载文件路径
    assert not pathlib.Path("simulate.py").exists()

def test_error_old_reference_not_in_code():
    """错误：活跃代码不再引用 simulate.py"""
    hits = []
    for p in pathlib.Path(".").rglob("*.py"):
        # 跳过本测试文件自身
        if p.name == "test_simulate_removal.py":
            continue
        if "simulate" in p.name:
            hits.append(str(p))
            continue
        # 跳过 .pyc / __pycache__
        if "__pycache__" in str(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except:
            continue
        # 检查是否仍有对 simulate.py 的硬编码引用（import 或路径）
        if "simulate.py" in text and "test_simulate_removal" not in text:
            hits.append(f"{p}:{text.count('simulate.py')}")
    # 允许历史产物中提及，但 py 代码不应再有
    assert hits == [], f"仍有代码引用 simulate.py：{hits}"

def test_static_no_simulate_in_tests():
    """静态：tests 不应再依赖 simulate.py"""
    for p in pathlib.Path("tests").rglob("*.py"):
        if p.name == "test_simulate_removal.py":
            continue
        text = p.read_text(encoding="utf-8")
        assert "simulate" not in text.lower() or "sim/" in text.lower() or "simulation" in text.lower(), f"{p} 仍含 simulate"
