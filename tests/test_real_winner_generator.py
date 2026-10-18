"""回归测试：一阶真实胜者生成器的样本目录扫描。"""
import json
import sys


def test_real_winner_generator_scans_existing_samples_without_name_error(tmp_path, monkeypatch):
    from sim import produce_real_winners

    monkeypatch.setattr(produce_real_winners, "WINNER_DIR", str(tmp_path))
    (tmp_path / "winner_01.json").write_text(
        json.dumps({"candidates": []}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "winner_03.json").write_text(
        json.dumps({"origin": {"seed": 17}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "produce_real_winners.py", "--count", "0", "--max-games", "0"
    ])

    produce_real_winners.main()

    # 扫描旧格式文件时应安全跳过其来源信息，而不是覆盖或删除既有数据。
    assert (tmp_path / "winner_01.json").exists()
    assert (tmp_path / "winner_03.json").exists()
