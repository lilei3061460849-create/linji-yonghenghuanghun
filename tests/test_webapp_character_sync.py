"""角色卡 GitHub 同步（webapp/character_sync.py）的单元测试。

同步会真的提交/推送，所以这里**不碰真仓库**：把 ``_git`` 换成假实现，
只验证状态汇报、成功口径与失败兜底（失败时本机数据仍在）。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp import character_sync


def _ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr="boom", code=1):
    return subprocess.CompletedProcess([], code, "", stderr)


def test_status_reports_non_git_directory(monkeypatch):
    monkeypatch.setattr(character_sync, "_git", lambda *args: _fail("not a git repository"))
    info = character_sync.status()
    assert info["git"] is False
    assert "Git 仓库" in info["error"]


def test_sync_marks_a_failed_push_without_losing_local_data(monkeypatch, tmp_path):
    monkeypatch.setattr(character_sync, "ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "player_characters.json").write_text(
        '{"version": 1, "characters": []}', encoding="utf-8")

    def fake_git(*args):
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return _ok("true")
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return _ok("test-branch")
        if args[:2] == ("config", "user.email"):
            return _ok("tester@example.com")
        if args[0] == "add":
            return _ok()
        if args[0] == "diff":
            return _fail("", code=1)          # 有改动待提交
        if args[0] == "commit":
            return _ok()
        if args[0] == "push":
            return _fail("! [rejected] non-fast-forward")
        return _ok()

    monkeypatch.setattr(character_sync, "_git", fake_git)
    result = character_sync.sync("测试")
    assert result["ok"] is False
    assert result["committed"] is True
    assert "推送失败" in result["message"]
    assert (tmp_path / "data" / "player_characters.json").exists()


def test_sync_reports_success_and_skips_empty_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(character_sync, "ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "player_characters.json").write_text(
        '{"version": 1, "characters": []}', encoding="utf-8")
    seen = []

    def fake_git(*args):
        seen.append(args)
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return _ok("true")
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return _ok("test-branch")
        if args[:2] == ("rev-parse", "--short"):
            return _ok("abc1234")
        if args[:2] == ("config", "user.email"):
            return _ok("tester@example.com")
        if args[0] == "diff":
            return _ok()                       # 没有新改动：不重复提交
        return _ok()

    monkeypatch.setattr(character_sync, "_git", fake_git)
    result = character_sync.sync("测试")
    assert result["ok"] is True
    assert result["committed"] is False
    assert result["commit"] == "abc1234"
    assert not any(args[0] == "commit" for args in seen)
