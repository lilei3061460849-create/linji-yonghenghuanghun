"""把角色卡同步进 GitHub：仓库分支上的一个 JSON 文件。

为什么这么做：浏览器里的预览沙箱是临时的，换手机/换会话就没了；而
``data/player_characters.json`` 提交进仓库后，数据就跟着 GitHub 走。

同步口径：

* 只提交 ``data/player_characters.json`` 这一个路径（``git commit -- <path>``），
  不会把工作区里其它未完成的改动一起带上；
* 推到**当前分支**（本会话分支），不切分支、不推到 main；
* 任何一步失败都返回 ``ok=False`` 和可读原因，本机文件仍然保留——
  「保存成功但同步失败」是一种正常状态，不是数据丢失；
* 仓库是公开的，所以角色卡内容会公开可见；不要在里面写真实姓名等隐私。
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELATIVE_PATH = "data/player_characters.json"
TIMEOUT_SECONDS = 30

_LOCK = threading.Lock()
_LAST: dict[str, Any] = {"ok": None, "at": "", "message": "尚未同步"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=TIMEOUT_SECONDS)


def _identity_args() -> list[str]:
    """仓库没配提交身份时给一个固定署名，避免自动存档因为缺身份而失败。"""
    try:
        email = _git("config", "user.email").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        email = ""
    if email:
        return []
    return ["-c", "user.name=arena-save",
            "-c", "user.email=arena-save@users.noreply.github.com"]


def _set_last(payload: dict[str, Any]) -> dict[str, Any]:
    _LAST.update(payload)
    _LAST["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return dict(_LAST)


def status() -> dict[str, Any]:
    """当前是否可同步：仓库、分支、文件是否已被 Git 跟踪、最近一次同步结果。"""
    info: dict[str, Any] = {
        "file": RELATIVE_PATH,
        "exists": (ROOT / RELATIVE_PATH).exists(),
        "last": dict(_LAST),
    }
    try:
        inside = _git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            info.update({"git": False, "error": "当前目录不是 Git 仓库"})
            return info
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        tracked = _git("ls-files", "--error-unmatch", RELATIVE_PATH)
        dirty = _git("status", "--porcelain", "--", RELATIVE_PATH).stdout.strip()
        info.update({
            "git": True,
            "branch": branch,
            "tracked": tracked.returncode == 0,
            "dirty": bool(dirty),
        })
    except (OSError, subprocess.SubprocessError) as exc:
        info.update({"git": False, "error": f"调用 git 失败：{exc}"})
    return info


def sync(reason: str = "手动同步") -> dict[str, Any]:
    """提交并推送角色卡文件；返回 ok / 分支 / 提交号 / 失败原因。"""
    path = ROOT / RELATIVE_PATH
    with _LOCK:
        try:
            inside = _git("rev-parse", "--is-inside-work-tree")
            if inside.returncode != 0:
                return _set_last({"ok": False, "error": "当前目录不是 Git 仓库",
                                  "message": f"{reason}：跳过了 GitHub 同步"})
            branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if not path.exists():
                return _set_last({"ok": False, "branch": branch,
                                  "error": "本机还没有角色卡文件",
                                  "message": "没有可同步的角色卡"})

            added = _git("add", "--", RELATIVE_PATH)
            if added.returncode != 0:
                return _set_last({"ok": False, "branch": branch,
                                  "error": (added.stderr or added.stdout).strip(),
                                  "message": "同步失败：git add 被拒绝"})

            staged = _git("diff", "--cached", "--quiet", "--", RELATIVE_PATH)
            committed = False
            if staged.returncode != 0:
                message = f"存档：角色卡（{reason}）"
                commit = _git(*_identity_args(), "commit", "-m", message,
                              "--", RELATIVE_PATH)
                if commit.returncode != 0:
                    return _set_last({"ok": False, "branch": branch,
                                      "error": (commit.stderr or commit.stdout).strip(),
                                      "message": "同步失败：提交被拒绝"})
                committed = True

            push = _git("push", "origin", branch)
            if push.returncode != 0:
                detail = (push.stderr or push.stdout).strip().splitlines()
                return _set_last({
                    "ok": False, "branch": branch, "committed": committed,
                    "pushed": False,
                    "error": " / ".join(detail[-2:]) if detail else "git push 失败",
                    "message": "已提交到本机 Git，但推送失败（联网后点『同步』重试）",
                })

            tip = _git("rev-parse", "--short", "HEAD").stdout.strip()
            return _set_last({
                "ok": True, "branch": branch, "committed": committed, "pushed": True,
                "commit": tip,
                "message": (f"已推送到 GitHub：{branch} @ {tip}" if committed
                            else f"GitHub 上已是最新（{branch} @ {tip}）"),
            })
        except subprocess.TimeoutExpired:
            return _set_last({"ok": False, "error": "git 超时",
                              "message": "同步超时，请稍后重试"})
        except (OSError, subprocess.SubprocessError) as exc:
            return _set_last({"ok": False, "error": str(exc),
                              "message": f"同步失败：{exc}"})
