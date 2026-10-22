"""效果链审计工具：静态调用图 + 运行时嵌套深度实测。

这个脚本只为**调查与验证**存在，不参与任何规则判定，也不被引擎导入。

    .venv/bin/python sim/effect_chain_audit.py sinks     # 状态变更汇点及其调用者
    .venv/bin/python sim/effect_chain_audit.py back X    # 反向调用链（谁可能触发 X）
    .venv/bin/python sim/effect_chain_audit.py fanout    # 出度最高的函数（巨石候选）
    .venv/bin/python sim/effect_chain_audit.py depth     # 多局实测嵌套深度（较慢）

用途：
  * Phase 2 调查（当前 Effect/Action/Trigger 的真实调用关系）用 `sinks`/`back`；
  * Phase 5 阈值依据用 `depth`——保险丝该设多大，由实测峰值决定，不靠拍脑袋。
"""
from __future__ import annotations

import ast
import random
import sys
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 状态变更「汇点」：真正改数值 / 发事件 / 分发触发的统一入口。
#: 这张表是**引擎现状的实测清单**（不是设计目标），新增汇点时应同步。
SINKS = {
    "engine/combat_parts/damage_death.py": [
        "_apply_hostile_damage", "_apply_hostile_damage_inner", "_raw_hp_loss",
        "_apply_blood_limit_change", "_check_hp_zero_death", "_on_entity_death",
        "_spawn_fenlie_clones", "_record_hp_loss_event", "_write_hp_loss_record",
    ],
    "engine/models.py": ["apply_heal", "take_damage", "gain_shield", "add_status",
                         "add_mutation", "emit_combat_event", "depart_battle"],
    "engine/combat_parts/cost_payment.py": ["pay_numeric_cost", "_lose_current_speed",
                                            "_gain_speed"],
    "engine/combat_parts/daowen_effect.py": ["apply_daowen_effect"],
    "engine/combat_parts/monster_phase.py": ["resolve_monster_phase",
                                             "_resolve_monster_daowen_choice"],
    "engine/combat_parts/monster_life.py": ["execute_evolution", "_proliferate_monster",
                                            "_cancer_character", "_sculpture_monster",
                                            "settle_victory_paths"],
    "engine/combat_parts/spells.py": ["resolve_daowen_trigger_spells",
                                      "resolve_attack_trigger_spells"],
    "engine/combat_hooks.py": ["apply_multiplier_adjust", "apply_incoming_adjust",
                               "apply_redirection", "apply_before_damage",
                               "apply_after_damage"],
    "engine/mechanisms/triggers.py": ["dispatch"],
    "engine/mechanisms/verbs.py": ["apply_verb"],
}


def _source_files() -> list[Path]:
    return sorted(list((ROOT / "engine").rglob("*.py"))
                  + list((ROOT / "sim").rglob("*.py")))


def _qual(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def build_index():
    defs: dict[tuple[str, str], set[str]] = {}
    callers: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (_qual(path), node.name)
                calls = _called_names(node)
                defs[key] = calls
                for callee in calls:
                    callers[callee].add(key)
    return defs, callers


def cmd_sinks(defs, callers) -> int:
    for path, names in SINKS.items():
        for name in names:
            c = sorted(callers.get(name, ()))
            print(f"\n### {name}  ({path})  —— 被 {len(c)} 处调用")
            for file, func in c[:16]:
                print(f"      {func}  @{file}")
    return 0


def cmd_back(defs, callers, targets) -> int:
    for target in targets:
        print(f"\n=== 反向调用链: {target} ===")
        seen, queue = set(), deque([(target, 0, [target])])
        while queue:
            name, depth, path = queue.popleft()
            if depth >= 6 or name in seen:
                continue
            seen.add(name)
            for file, func in sorted(callers.get(name, ())):
                new_path = path + [f"{func}@{file}"]
                print("  " + "  " * depth + " <- ".join(new_path))
                queue.append((func, depth + 1, new_path))
    return 0


def cmd_fanout(defs, callers) -> int:
    rows = sorted(((len(v), k) for k, v in defs.items()), reverse=True)
    print("出度最高的函数（调用别人最多 = 最像巨石）：")
    for count, (file, name) in rows[:25]:
        print(f"{count:5d}  {name:45s} @{file}")
    return 0


# --------------------------------------------------------------- 运行时实测

def cmd_depth(defs, callers) -> int:
    """多局实测嵌套深度与再入峰值。

    阈值依据：保险丝应显著高于实测峰值（否则正常组合会被误伤）。
    """
    sys.path.insert(0, str(ROOT))
    import os
    os.chdir(ROOT)
    from engine.combat import CombatEngine
    from engine.mechanisms.triggers import TriggerBus
    from engine.models import Entity, GameState
    from sim import build_learner as bl

    stats = {"max_depth": 0, "per": {}, "counts": {}, "fuse_peak": 0}
    depth = {"d": 0}

    def wrap(cls, name):
        original = getattr(cls, name)
        key = f"{cls.__name__}.{name}"

        def inner(*args, **kwargs):
            depth["d"] += 1
            stats["max_depth"] = max(stats["max_depth"], depth["d"])
            stats["per"][key] = max(stats["per"].get(key, 0), depth["d"])
            stats["counts"][key] = stats["counts"].get(key, 0) + 1
            try:
                return original(*args, **kwargs)
            finally:
                depth["d"] -= 1

        setattr(cls, name, inner)

    for cls, names in (
        (CombatEngine, ["_apply_hostile_damage", "_check_hp_zero_death",
                        "_on_entity_death", "pay_numeric_cost", "apply_daowen_effect",
                        "_raw_hp_loss", "_apply_blood_limit_change",
                        "_spawn_fenlie_clones"]),
        (GameState, ["apply_heal"]),
        (Entity, ["add_status", "add_mutation", "gain_shield", "depart_battle"]),
        (TriggerBus, ["dispatch"]),
    ):
        for name in names:
            if hasattr(cls, name):
                wrap(cls, name)

    cases = [("杀伐", ["再生", "庇护", "束缚", "贯穿", "固执"]),
             ("坠落", ["杀伐", "连击", "再生", "庇护", "透支"]),
             ("封印", ["杀伐", "再生", "庇护", "贯穿", "固执"])]
    games = 0
    for region in ("扭曲都市", "罪孽都市", "龙心谷"):
        for starter, learn in cases:
            for seed in (1, 2, 3):
                bl.play(starter, learn, region, seed=seed, rng=random.Random(seed))
                games += 1

    print(f"跑了 {games} 局")
    print("最大调用嵌套深度:", stats["max_depth"])
    print("各汇点最深嵌套（前 12）:")
    for key, value in sorted(stats["per"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"   深{value:3d}  调用{stats['counts'][key]:6d} 次   {key}")
    return 0


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    mode = argv[0]
    defs, callers = build_index()
    if mode == "sinks":
        return cmd_sinks(defs, callers)
    if mode == "back":
        return cmd_back(defs, callers, argv[1:])
    if mode == "fanout":
        return cmd_fanout(defs, callers)
    if mode == "depth":
        return cmd_depth(defs, callers)
    print(f"未知模式: {mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
