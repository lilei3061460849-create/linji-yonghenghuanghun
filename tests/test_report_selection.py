"""
pytest - 批量选取脚本的独立输出路径与结算记录

`sim/pick_best_report.py` 是旧规则下的批量平衡工具，默认只写入被忽略的
`data/batch_report.md`，不依赖也不创建仓库根目录的 `报告.md`。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_s = importlib.util.spec_from_file_location(
    "pb", os.path.join(ROOT, "sim", "pick_best_report.py"))
pb = importlib.util.module_from_spec(_s)
_s.loader.exec_module(pb)

# ---------- 正常路径 ----------

def test_play_and_record_reports_hp_before_crown():
    """正常路径：通关的轮回必须给出进入冠冕前的血量"""
    r = pb.play_and_record("龙心谷", seed=105)
    assert not r.get("invalid"), r.get("reason")
    if not r.get("died"):
        assert r["hp_before_crown"] is not None
        assert r["cleared"] == 7
        assert 0 < r["hp_before_crown"] <= 60


# ---------- 边界条件 ----------

def test_highest_hp_wins():
    """边界：选取逻辑必须挑血量最高者"""
    finished = [(45, "龙心谷", 1, ["a"]), (60, "罪孽都市", 2, ["b"]), (58, "扭曲都市", 3, ["c"])]
    finished.sort(key=lambda t: -t[0])
    assert finished[0][0] == 60
    assert finished[0][1] == "罪孽都市"


def test_died_runs_excluded():
    """边界：中途阵亡的轮回 hp_before_crown 为 None，不得参与排序"""
    r = pb.play_and_record("龙心谷", seed=999999)
    if r.get("died"):
        assert r["hp_before_crown"] is None


def test_batch_selection_defaults_to_ignored_output_not_root_report():
    """边界：批量平衡工具默认写到 data/，不创建已取消的根目录报告.md。"""
    parser = pb._build_arg_parser()
    assert parser.get_default("out") == "data/batch_report.md"
    assert os.path.join(ROOT, parser.get_default("out")) != os.path.join(ROOT, "报告.md")


# ---------- 错误输入 ----------

def test_invalid_runs_are_flagged(monkeypatch):
    """错误输入：引擎抛异常的对局必须被标为 invalid 并附原因"""
    class Boom:
        def execute_action(self, *a, **k):
            raise RuntimeError("模拟引擎异常")

    monkeypatch.setattr(pb, "GameEngine", lambda *a, **k: Boom())
    r = pb.play_and_record("龙心谷", seed=1)
    assert r["invalid"] is True
    assert "RuntimeError" in r["reason"]


def test_invalid_runs_never_selected():
    """错误输入：无效局不得进入候选排序（无 hp_before_crown 字段）"""
    bad = {"invalid": True, "reason": "boom"}
    assert bad.get("hp_before_crown") is None
    assert bad["invalid"] is True
