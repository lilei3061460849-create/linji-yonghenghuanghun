#!/usr/bin/env python3
"""生成《行动分组清单》：把 `_dispatch_action` 的每条路由按职责分组落成文档。

只做静态分析，不导入引擎、不运行游戏：

* action → 处理器方法：解析 `engine/api.py::_dispatch_action` 里的
  `if action_type == "...": return self._xxx(params)`（含注册表已登记的条目）；
* 处理器行数：`ast` 定位方法定义的起止行；
* 读写的状态字段：在方法源码里 grep `self.state.<字段>` 与 `state.<字段>`；
* 是否改全局状态：出现 `save_game` / `self.dice` / `self.event_pool` /
  `self._action_history` / `self._pending_interrupts` / `self.combat.` 即为是。

用法：

    python3 tools/gen_action_inventory.py [--out 内部/行动分组清单-2026-11-03.md]
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "engine" / "api.py"

GROUP_ORDER = [
    "开放世界地图",
    "开局与局外",
    "战斗回合与攻击",
    "道纹与法术",
    "遗物与消耗品",
    "事件与裁定",
    "同伴与雇佣",
    "终音法器与龙族",
    "自定义资源",
    "查询与元信息",
    "其它",
]

# 名字没有统一前缀、无法按前缀归类的，手工归类（新增 action 时记得补一行）。
ACTION_GROUP = {
    "activate_duel_relic": "终音法器与龙族",
    "blood_feast": "终音法器与龙族",
    "choose_first_embrace": "终音法器与龙族",
    "choose_terminal_artifact": "终音法器与龙族",
    "declare_fuyuebei_toll": "终音法器与龙族",
    "declare_tail_sacrifice": "终音法器与龙族",
    "devour_monster": "终音法器与龙族",
    "enslave_as_chizu": "终音法器与龙族",
    "fire_godfather_revolver": "终音法器与龙族",
    "select_shared_dragon_heart": "终音法器与龙族",
    "unlock_dragon_trait": "终音法器与龙族",
    "use_black_card": "终音法器与龙族",
    "use_crime_vault": "终音法器与龙族",
    "use_truth_eye": "终音法器与龙族",
    "command_ally": "同伴与雇佣",
    "deploy_employee": "同伴与雇佣",
    "dismiss_employee": "同伴与雇佣",
    "pay_employee_wage": "同伴与雇佣",
    "repay_debt_employee": "同伴与雇佣",
    "lianxin_in_battle": "战斗回合与攻击",
    "monster_phase": "战斗回合与攻击",
    "redeem_attribute_points": "开局与局外",
}

# action 名前缀 → 分组。新增 action 落在"其它"说明这张表要补一行。
GROUP_BY_PREFIX = [
    ("world_", "开放世界地图"),
    ("setup_", "开局与局外"),
    ("pre_battle_action", "开局与局外"),
    ("timed_world_pre_battle", "开局与局外"),
    ("resolve_redemption", "开局与局外"),
    ("upgrade_doctor", "开局与局外"),
    ("battle_start", "战斗回合与攻击"),
    ("battle_end", "战斗回合与攻击"),
    ("round_", "战斗回合与攻击"),
    ("prepare_attack", "战斗回合与攻击"),
    ("resolve_attack", "战斗回合与攻击"),
    ("attack", "战斗回合与攻击"),
    ("declare_", "战斗回合与攻击"),
    ("resolve_monster_phase", "战斗回合与攻击"),
    ("prepare_monster_phase", "战斗回合与攻击"),
    ("resolve_final_duel", "战斗回合与攻击"),
    ("resolve_ally_phases", "战斗回合与攻击"),
    ("restart_battle", "战斗回合与攻击"),
    ("focus", "战斗回合与攻击"),
    ("restart", "战斗回合与攻击"),
    ("use_daowen", "道纹与法术"),
    ("cast", "道纹与法术"),
    ("define_spell", "道纹与法术"),
    ("undefine_spell", "道纹与法术"),
    ("use_resonance", "道纹与法术"),
    ("assign_learned_daowen", "道纹与法术"),
    ("choose_diandang_daowen", "道纹与法术"),
    ("choose_hired_daowen", "道纹与法术"),
    ("consume_item", "遗物与消耗品"),
    ("set_active_relics", "遗物与消耗品"),
    ("toggle_relic", "遗物与消耗品"),
    ("choose_discovered_relic", "遗物与消耗品"),
    ("discard_discovered_relic", "遗物与消耗品"),
    ("choose_discovered_item", "遗物与消耗品"),
    ("discard_discovered_item", "遗物与消耗品"),
    ("choose_terminal_artifact", "遗物与消耗品"),
    ("choose_first_embrace", "遗物与消耗品"),
    ("resolve_event", "事件与裁定"),
    ("read_death_book", "事件与裁定"),
    ("submit_ruling", "事件与裁定"),
    ("define_resource", "自定义资源"),
    ("activate_resource", "自定义资源"),
    ("list_resources", "自定义资源"),
]

ROUTE_RE = re.compile(
    r'if action_type == "(?P<name>[A-Za-z0-9_]+)":\s*\n\s*return self\.(?P<method>[A-Za-z0-9_]+)\(',
    re.MULTILINE,
)
REGISTRY_RE = re.compile(r'@ACTION_REGISTRY\.action\(\s*"(?P<name>[A-Za-z0-9_]+)"[^)]*?\bgroup="(?P<group>[^"]+)"',
                         re.DOTALL)
STATE_RE = re.compile(r"\bstate\.([A-Za-z_][A-Za-z0-9_]*)")
GLOBAL_TOKENS = ("save_game", "self.dice", "self.event_pool", "self._action_history",
                 "self._pending_interrupts", "self.combat.", "self._last_result")


def group_for(name: str, registered_group: str | None = None) -> str:
    if registered_group and registered_group in GROUP_ORDER:
        return registered_group
    if name in ACTION_GROUP:
        return ACTION_GROUP[name]
    for prefix, group in GROUP_BY_PREFIX:
        if name.startswith(prefix):
            return group
    return "其它"


DIRECT_RETURN_RE = re.compile(
    r'(?:el)?if action_type == "(?P<name>[A-Za-z0-9_]+)":\s*\n\s*return \{',
    re.MULTILINE,
)


def collect_routes(api_source: str) -> dict[str, str]:
    """action → 处理器方法名；无处理器的直接返回路由记为 ``（直接返回）``。"""
    routes: dict[str, str] = {}
    for match in ROUTE_RE.finditer(api_source):
        routes[match.group("name")] = match.group("method")
    # 还有 `elif action_type == "...":` 形式
    for match in re.finditer(
            r'elif action_type == "(?P<name>[A-Za-z0-9_]+)":\s*\n\s*return self\.(?P<method>[A-Za-z0-9_]+)\(',
            api_source):
        routes[match.group("name")] = match.group("method")
    for match in DIRECT_RETURN_RE.finditer(api_source):
        routes.setdefault(match.group("name"), "（直接返回）")
    return routes


def collect_registry(engine_dir: Path) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for path in engine_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in REGISTRY_RE.finditer(text):
            found[match.group("name")] = (match.group("group"), str(path.relative_to(ROOT)))
    return found


def method_spans(engine_dir: Path) -> dict[str, tuple[str, int, int, str]]:
    """方法名 → (文件, 起始行, 行数, 源码)。同名以 api.py 之外的 handler 优先。"""
    spans: dict[str, tuple[str, int, int, str]] = {}
    for path in sorted(engine_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            body = "\n".join(text.splitlines()[start - 1:end])
            rel = str(path.relative_to(ROOT))
            previous = spans.get(node.name)
            if previous is None or (previous[0] == "engine/api.py" and rel != "engine/api.py"):
                spans[node.name] = (rel, start, end - start + 1, body)
    return spans


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="内部/行动分组清单-2026-11-03.md")
    args = parser.parse_args()

    api_source = API.read_text(encoding="utf-8")
    routes = collect_routes(api_source)
    registry = collect_registry(ROOT / "engine")
    spans = method_spans(ROOT / "engine")

    rows = []
    for name in sorted(routes):
        method = routes[name]
        location, start, length, body = spans.get(method, ("（未找到）", 0, 0, ""))
        fields = sorted(set(STATE_RE.findall(body)))
        touches_global = any(token in body for token in GLOBAL_TOKENS)
        rows.append({
            "name": name, "method": method, "group": group_for(name, registry.get(name, (None,))[0]),
            "file": location, "start": start, "length": length,
            "fields": fields, "global": touches_global,
            "registered": name in registry,
        })

    lines = [
        "# 行动分组清单（自动生成）",
        "",
        "由 `python3 tools/gen_action_inventory.py` 生成，**不要手改**。",
        "数据来源：`engine/api.py::_dispatch_action` 的路由 + `engine/handlers/` 下",
        "`@ACTION_REGISTRY.action(...)` 的注册。仅静态分析，不运行游戏。",
        "",
        "`state 字段` 是对方法源码里 `state.<字段>` 的 grep，不代表完整读写集；",
        "`改全局状态=是` 指方法体里出现 `save_game` / `self.dice` / `self.event_pool` /",
        "`self._action_history` / `self._pending_interrupts` / `self.combat.*` / `self._last_result`。",
        "`已注册` 表示该 action 已迁到注册表（`_dispatch_action` 走查表路径）。",
        "",
        f"合计 **{len(rows)}** 个 action，其中已注册 **{sum(1 for r in rows if r['registered'])}** 个。",
        "",
    ]

    for group in GROUP_ORDER:
        members = [row for row in rows if row["group"] == group]
        if not members:
            continue
        lines.append(f"## {group}（{len(members)} 个，共 "
                     f"{sum(row['length'] for row in members)} 行）")
        lines.append("")
        lines.append("| action | 处理器 | 文件:起始行 | 行数 | 改全局状态 | 已注册 | 读写 state 字段 |")
        lines.append("| --- | --- | --- | ---: | --- | --- | --- |")
        for row in members:
            where = f"`{row['file']}:{row['start']}`" if row["file"] != "（未找到）" else "（未找到）"
            fields = "、".join(f"`{f}`" for f in row["fields"][:10]) or "—"
            if len(row["fields"]) > 10:
                fields += " …"
            lines.append(f"| `{row['name']}` | `{row['method']}` | {where} | {row['length']} | "
                         f"{'是' if row['global'] else '否'} | {'✅' if row['registered'] else ''} | {fields} |")
        lines.append("")

    unclassified = [row for row in rows if row["group"] == "其它"]
    if unclassified:
        lines.append(f"## 需要归类（{len(unclassified)} 个在 `GROUP_BY_PREFIX` 里没有规则）")
        lines.append("")
        for row in unclassified:
            lines.append(f"- `{row['name']}` → `{row['method']}`（{row['length']} 行）")
        lines.append("")

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}: {len(rows)} actions, "
          f"{sum(1 for r in rows if r['registered'])} registered, "
          f"{len(unclassified)} unclassified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
