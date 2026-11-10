#!/usr/bin/env python3
"""副本草案面板合规审计（只读）。

怪物设计只约束**属性点数**与**道纹数量**，道纹 X 值自由自定义。
旧的「道纹总值」配额已废止——它本是"怪物发动道纹不支付法力"的补丁；怪物改为支付法力后，
真正的约束是[法限]构成每回合法力预算，总值限制既多余又会与预算打架。
二阶草案：乱葬岗/沉沦海 属性点100，道纹5条。
永夜庭：固定场次属性点=60×N（血族机制），特殊豁免面板审计。

面板成本 = ⌈血限/6⌉ + 2×攻击次数 + 2×攻击力（与轮回者属性点口径一致）。
用法: python sim/audits/audit_dungeons.py
"""

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import math
import re
import sys

TARGETS = {
    "乱葬岗": {"budget": 100, "dw_count": 5},
    "沉沦海": {"budget": 100, "dw_count": 5},
    "永夜庭": None,  # 特殊属性点机制，豁免
}
# 非普通池怪：事件boss/员工面板等，豁免面板与道纹配额审计
SPECIAL_MONSTERS = {"疫巢", "潜水员"}


def parse_monsters(path: str) -> list[dict]:
    text = open(path, encoding="utf-8").read()
    out = []
    for line in text.splitlines():
        # 面板与轮回者同口径，写作「名字（[血限]/[法限]/[速限]，道纹…）」
        m = re.match(r"^([\u4e00-\u9fff]+)（(\d+)/(\d+)/(\d+)，(.+)）", line)
        if not m:
            continue
        name, hp, ml, sl, dw_raw = m.groups()
        # 面板不再写死 X，道纹段写作「分裂，狂暴，…」。
        # 解析必须与 engine/monsters.py 同口径、且同样**兼容带 X 的旧写法**，
        # 否则面板迁移后审计会把道纹数读成 0、误报"道纹数≠5"。
        # 按「，」切词而非 findall：后者会把行内说明文字里的汉字+数字也当道纹。
        dw = {}
        for token in re.split(r"[，,、]", dw_raw):
            mt = re.match(r"^\s*([\u4e00-\u9fff]{2,4})(\d+)?\s*$", token.strip())
            if mt:
                dw[mt.group(1)] = int(mt.group(2)) if mt.group(2) else None
        # 属性点计价不变：1点=6血限、2点=1法限=1速限（攻次=速限、攻力=法限）
        out.append({"name": name, "hp": int(hp), "ap": int(ml), "ac": int(sl), "dw": dw})
    return out


def panel_cost(hp, ap, ac):
    return math.ceil(hp / 6) + 2 * ac + 2 * ap


def audit():
    total_viol = 0
    for fname, spec in TARGETS.items():
        path = f"副本/{fname}.md"
        monsters = parse_monsters(path)
        print(f"=== {fname}（{len(monsters)}只） ===")
        if spec is None:
            print("  特殊属性点机制（60×场次），豁免面板审计\n")
            continue
        viol = 0
        for m in monsters:
            if m["name"] in SPECIAL_MONSTERS:
                print(f"  {m['name']:<8} （特殊面板，豁免审计）")
                continue
            cost = panel_cost(m["hp"], m["ap"], m["ac"])
            issues = []
            if cost > spec["budget"]:
                issues.append(f"面板成本{cost}>{spec['budget']}")
            if len(m["dw"]) != spec["dw_count"]:
                issues.append(f"道纹数{len(m['dw'])}≠{spec['dw_count']}")
            status = "合规" if not issues else "❌" + "；".join(issues)
            if issues:
                viol += 1
            # X 为 None 表示面板未写死、发动时自选，打印时只显示道纹名
            dw_s = "+".join(f"{k}{'' if v is None else v}" for k, v in m["dw"].items())
            print(f"  {m['name']:<8} {m['ac']}×{m['ap']}/{m['hp']:<5} {dw_s:<35} 成本{cost:<4} {status}")
        print(f"  违规 {viol}/{len(monsters)}\n")
        total_viol += viol
    print(f"总违规: {total_viol}")
    return total_viol


if __name__ == "__main__":
    sys.exit(1 if audit() else 0)