#!/usr/bin/env python3
"""生成 全道纹索引.md（一次性工具：道纹数据源变更后重跑即可）。

数据源（全部取当前版本引擎/文档事实，不抄旧数据）：
- 效果正文/支付方式：已迁移数值规则取 engine/rule_engine.py 的统一定义，其余取 engine/daowen.py DaoWenEngine.calculate_* docstring
- 归属分类：engine/gamedata.py（SHAFA_LOOP_DAOWEN / ORIGINAL / TRANSFORM / REGION_EXCLUSIVE / UNIMPLEMENTED）
- 残韵闭环：engine/daowen.py DaoWenEngine.CLOSED_LOOPS
- 承载怪物：副本/*.md 全部怪物面板行（怪物池 + 事件/雇佣面板）
"""
import inspect
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.daowen import DaoWenEngine, ResonanceEngine, display_resonance_node  # noqa: E402
from engine.rule_engine import DAOWEN_RULES, UNMIGRATED_REASON  # noqa: E402
from engine.gamedata import (  # noqa: E402
    MONSTER_TRANSFORM_DAOWEN,
    ORIGINAL_MONSTER_DAOWEN,
    REGION_EXCLUSIVE_DAOWEN,
    REGION_TIERS,
    SHAFA_LOOP_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN,
    FIRST_EMBRACE_DAOWEN,
)
from engine.dungeons import load_dungeon_documents  # noqa: E402

DaoWenEngine.register_all()

# 面板行两种历史格式都要认：旧 `名字（血限×法限/速限，道纹…）`、新 `名字（血限/法限/速限，道纹…）`。
# （2026-10-03 修：旧正则只认旧的 × 格式，导致「承载怪物」整列被清空。）
PANEL = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)(?:[×x](\d+)|/(\d+))/(\d+)(?:[，,]([^)）\n]*))?[）)]')

# ---------- 采集 ----------
effects, costs, params_of = {}, {}, {}
for name, fn in DaoWenEngine._registry.items():
    rule_definition = DAOWEN_RULES.get(name)
    if rule_definition is not None:
        raw_cost = rule_definition.cost_text("X")
        if rule_definition.cost_type == "消耗":
            costs[name] = f"支付：法力{raw_cost.removeprefix('消耗')}"
        elif rule_definition.cost_type == "冷却":
            costs[name] = f"支付：冷却{raw_cost.removeprefix('冷却').removesuffix('场')}"
        else:
            costs[name] = f"支付：{raw_cost}"
        effects[name] = rule_definition.effect_text(
            x="X", target_name=None if rule_definition.default_subject == "自身" else "[目标]"
        ) + "。"
    else:
        doc = (fn.__doc__ or "").strip().splitlines()
        first = doc[0].strip()
        # 2026-10-03：容忍道纹名后的括注（如「全速X（原名【迟滞】）」「必中X（2026-09-28 二次更正）」），
        # 括注不参与解析；效果正文仍取紧随其后的第一段。
        m = re.match(r'^\S+?X(?:/[A-Za-z]+)?(?:（[^）]*）)?[：:](.+?)。(.*)$', first)
        assert m, f"{name}: docstring 格式无法解析: {first!r}"
        costs[name] = m.group(1)
        eff = m.group(2)
        effects[name] = (eff + "。") if eff and not eff.endswith("。") else eff
    params_of[name] = list(inspect.signature(fn).parameters)

# 承载怪物：扫描全部已实现副本文档的面板行（怪物池+事件/雇佣）
carriers = defaultdict(list)
for region, text in sorted(load_dungeon_documents().items()):
    for line in text.splitlines():
        m = PANEL.match(line.strip())
        if m and m.group(6):
            # 新面板格式的道纹列不带次数（如「畸变，坏死，狂暴」），旧格式带次数（如「执念2」）。
            # 两种都解析：有次数就带次数，没有就只记怪物名。
            for token in re.split(r'[，,、]', m.group(6)):
                token = token.strip()
                mm = re.match(r'^([\u4e00-\u9fff·]+?)(\d+)?$', token)
                if not mm or mm.group(1) not in DaoWenEngine._registry:
                    continue
                entry = f"{m.group(1)}{mm.group(2) or ''}"
                if entry not in carriers[mm.group(1)]:
                    carriers[mm.group(1)].append(entry)

# 残韵边（双向：2026-10-07 用户令——同一条边正反两向都走，同一种残韵、同样消耗一次）
out_edges = defaultdict(list)
for _name in DaoWenEngine._registry:
    for _p in ResonanceEngine.get_available_resonance(_name):
        out_edges[_name].append(
            f"（{_p['resonance_type']}）→{_p['target_daowen']}"
            f"（{'正向' if _p['direction'] == '正向' else '反向'}）")
in_edges = defaultdict(list)

# 目标需求
def target_label(name):
    p = params_of[name]
    if "target" in p:
        return "需显式选定"
    # 【必中】的结算函数签名是 calculate_bizhong(x)，没有 target 形参，会被下面的
    # 兜底判成「自身」——那是 2026-09-28 的 self-buff 旧口径。2026-10-12 用户令增强后
    # 它是「你选中的[目标]无法进行回合外行动」，按规则文必须显式选定目标。
    if name == "必中":
        return "需显式选定"
    if name in ("波及", "血契"):
        return "X个（显式提交）"
    if name == "封印":
        return "X个怪物（引擎自动选定）"
    doc = effects[name]
    if "所有敌方" in doc:
        return "敌方全体"
    if re.search(r"所有|全场|场上所有", doc):
        return "全局"
    return "自身"

# 确定性顺序（2026-10-03 修）：REGION_EXCLUSIVE_DAOWEN 的值是 **set**，直接迭代会得到
# 随进程变化的哈希序——每次重新生成索引，副本专属段与总览表里的行序都会漂移。
# 这里改为确定口径：先按该副本闭环链的出场顺序，其余按名字排序补齐。
def region_daowen_order(region: str) -> list:
    ns = set(REGION_EXCLUSIVE_DAOWEN[region])
    chain: list = []
    for src, _rtype, dst in ResonanceEngine.CLOSED_LOOPS.get(f"{region}闭环", []):
        for n in (src, dst):
            if n in ns and n not in chain:
                chain.append(n)
    return chain + sorted(ns - set(chain))


# 分类
CATEGORY = {}
for n in SHAFA_LOOP_DAOWEN:
    CATEGORY[n] = "shaifa"
CATEGORY["血契"] = "shaifa"
for n in ORIGINAL_MONSTER_DAOWEN:
    CATEGORY[n] = "original"
for n in MONSTER_TRANSFORM_DAOWEN:
    CATEGORY[n] = "transform"
for region in REGION_EXCLUSIVE_DAOWEN:
    for n in region_daowen_order(region):
        CATEGORY[n] = region
for n in FIRST_EMBRACE_DAOWEN:
    CATEGORY[n] = "first_embrace"
for n, region in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.items():
    CATEGORY[n] = "unimpl"

assert set(CATEGORY) == set(DaoWenEngine._registry), (
    set(CATEGORY) ^ set(DaoWenEngine._registry))

# 特殊注记（引擎机制，非数值）
NOTES = {
    "消灾": "唯一允许局外发动的道纹（局外消耗×2）；重置随机数。",
    "封印": "玩家支付异变X，使一个显式选定的目标怪物延后X回合再入场；暂离仍阻塞战终，回场当回合沿用增援白板，最终命零正常进入死亡与碎片结算。",
    # 2026-10-11 修正：旧注记「【命零】时触发」是废止口径。沿革见
    # engine/daowen.py::calculate_fenlie —— 旧版[命零]触发（2026-09-17 废止）
    # → 即时发动+代价【衰老】X×10Y → 2026-10-08 用户令改为代价【流血】X×10Y。
    "分裂": "即时发动（不绑【命零】）；代价【流血】X×10Y 付的是当前生命，付不起不能发动。复制体无【分裂】道纹、无[碎片]奖励。",
    "招魂": "唤回者为[临时朋友]，[战终]消失。",
    "真理": "初拥之夜选项5授予的血族专属道纹。X=陈述句数：提出X句陈述，目标须逐句言明真理，任一句无法言明则无法开口，持续X回合；真伪由DM逐句裁定。冷却2场战斗。",
    "原初": "怪物困境时发动【原初X】可临时借用一种自身未持有的原始怪物道纹（仅借用，不获得）。",
}

def loop_chain(loop_name):
    edges = ResonanceEngine.CLOSED_LOOPS[loop_name]
    # 环：找起点（第一条边的src）并按边串联；遗物化节点在索引里统一显示「？？」
    first = display_resonance_node(edges[0][0])
    chain = [first]
    nxt = {s: (r, d) for s, r, d in edges}
    cur = edges[0][0]
    for _ in range(len(edges)):
        r, d = nxt[cur]
        chain.append(f"⇄（{r}）{display_resonance_node(d)}")
        cur = d
    return " ".join(chain)

# ---------- 渲染 ----------
L = []
A = L.append
A("# 全道纹索引")
A("")
A(f"本文件是**当前版本全部道纹（{len(DaoWenEngine._registry)}种）的完整索引**。道纹条目按归属分类：")
A(f"杀伐闭环（通用核心）{len(SHAFA_LOOP_DAOWEN)} ｜ 原始怪物道纹 {len(ORIGINAL_MONSTER_DAOWEN)} ｜ "
  f"怪物转化道纹 {len(MONSTER_TRANSFORM_DAOWEN)} ｜ 副本专属 "
  f"{'/'.join(str(len(region_daowen_order(r))) for r in ('扭曲都市', '罪孽都市', '龙心谷', '乱葬岗'))} ｜ "
  f"初拥专属 {len(FIRST_EMBRACE_DAOWEN)} ｜ 未实现 {len(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN)}。")
A("")
A("- **效果正文/支付方式**：已迁移道纹从 `engine/rule_engine.py` 统一定义生成；其余道纹取 `engine/daowen.py` `calculate_*` docstring。公式以实际引擎结算为准。")
A("- **归属与残韵闭环**：`engine/gamedata.py` + `engine/daowen.py`（`CLOSED_LOOPS`）；与规则正文的闭环图一致。\n"
  "  `CLOSED_LOOPS` 里的每条边为**无向边**（2026-10-07 用户令：残韵路径双向）——登记 `(a,类型,b)` 表示 a、b 互为相邻节点，\n"
  "  正反两向都走同一种残韵、同样消耗一次；双向由 `ResonanceEngine.find_transformations` 在查询时展开，边表不重复登记反向边。\n"
  "  闭环中的 `？？` 专用于道纹遗物化后的旧节点占位，`（待补）` 用于其它待补设计的空位；两类占位均不可作为残韵结果获得。")
A("- **承载怪物**：解析自 `副本/*.md` 全部面板行（12只怪物池 + 事件/雇佣面板，如「追求者」）；格式 `怪物名X`。")
A("- 冲突时：数值/结算以引擎为准，规则叙述以 [规则正文](规则正文.md#第四宇宙规则正文) 为准，本索引为派生索引（与两者冲突时应重新生成本文件）。")
A("- 通用规则（自由规则里的自由控X、[目标]与闪避、代价结算、平分、声明、怪物冷却道纹X≤1等）见 [规则正文](规则正文.md#第四宇宙规则正文)，本文件不重复。")
A("")
A("## 数值规则迁移状态")
A("")
migrated_names = [name for name in DAOWEN_RULES if name in DaoWenEngine._registry]
legacy_names = sorted(set(DaoWenEngine._registry) - set(migrated_names))
A(f"- **统一数值规则执行器**：{ '、'.join(migrated_names) }。这些道纹的数值、触发条件、当前值传递与摘要共用 `engine/rule_engine.py`。")
A(f"- **保留旧路径（未迁移）**：{ '、'.join(legacy_names) }。{UNMIGRATED_REASON}")
A("")
A("## 目录")
A("")
A("- [总览表](#总览表)")
A("- [杀伐闭环（通用核心·11）](#杀伐闭环通用核心11)")
A(f"- [原始怪物道纹（{len(ORIGINAL_MONSTER_DAOWEN)}）](#原始怪物道纹{len(ORIGINAL_MONSTER_DAOWEN)})")
A(f"- [怪物转化道纹（{len(MONSTER_TRANSFORM_DAOWEN)}）](#怪物转化道纹{len(MONSTER_TRANSFORM_DAOWEN)})")
for _r in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"):
    A(f"- [{_r}专属（{len(region_daowen_order(_r))}）](#{_r}专属{len(region_daowen_order(_r))})")
A(f"- [初拥专属（{len(FIRST_EMBRACE_DAOWEN)}）](#初拥专属{len(FIRST_EMBRACE_DAOWEN)})")
A("- [未实现（荒疫古城·1）](#未实现荒疫古城1)")
A("- [道纹归属与学习规则](#道纹归属与学习规则)")
A("")
A("---")
A("")

# 总览表
A("## 总览表")
A("")
A("| 道纹 | 分类 | 支付方式 | [目标] | 残韵路径（双向） | 承载怪物 |")
A("| --- | --- | --- | --- | --- | --- |")
CAT_LABEL = {
    "shaifa": "通用核心", "original": "原始", "transform": "转化",
    "扭曲都市": "扭曲专属", "罪孽都市": "罪孽专属", "龙心谷": "龙心专属",
    "乱葬岗": "乱葬专属", "first_embrace": "初拥专属", "unimpl": "未实现",
}
for name in sorted(DaoWenEngine._registry, key=lambda n: (list(CATEGORY).index(n), n)):
    pass
order = (list(SHAFA_LOOP_DAOWEN) + sorted(ORIGINAL_MONSTER_DAOWEN)
         + sorted(MONSTER_TRANSFORM_DAOWEN)
         + [n for r in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗") for n in region_daowen_order(r)]
         + sorted(FIRST_EMBRACE_DAOWEN)
         + list(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN))
for name in order:
    out = "、".join(out_edges[name]) if out_edges[name] else "—"
    c = carriers.get(name, [])
    cstr = "、".join(c[:6]) + (f"…+{len(c)-6}" if len(c) > 6 else "") or "—"
    A(f"| {name} | {CAT_LABEL[CATEGORY[name]]} | {costs[name]} | {target_label(name)} | {out} | {cstr} |")
A("")
A("> 承载怪物列按副本文档面板解析；「…」表示超过6种，逐条见下方分类小节。")
A("")
A("---")
A("")

def section(title, names, note_lines=(), loop=None, loop_title=None):
    A(f"## {title}")
    A("")
    for n in note_lines:
        A(n)
        A("")
    if loop:
        A(f"闭环路径：{loop_chain(loop)}")
        A("")
    for name in names:
        A(f"### {name}")
        A(f"X：{costs[name]}。{effects[name]}" if effects[name] else f"X：{costs[name]}。")
        meta = [f"[目标]：{target_label(name)}"]
        if out_edges[name]:
            meta.append("残韵路径（双向）：" + "、".join(out_edges[name]))
        c = carriers.get(name, [])
        meta.append("承载：" + ("、".join(c) if c else "—（无怪物承载）"))
        A(f"> {' ｜ '.join(meta)}")
        if name in NOTES:
            A(f"> 注：{NOTES[name]}")
        A("")

section("杀伐闭环（通用核心·11）", list(SHAFA_LOOP_DAOWEN),
        note_lines=["开局【发现】初始道纹只从本闭环抽（随机列出3种未持有候选，显式选1；杀伐不是默认起手）。",
                    "通用核心道纹为人类侧基础概念，局外可经学习/残韵获得。"],
        loop="杀伐闭环")

section(f"原始怪物道纹（{len(ORIGINAL_MONSTER_DAOWEN)}）", sorted(ORIGINAL_MONSTER_DAOWEN),
        note_lines=[
            "原始怪物道纹是各转化分支的起点：**不消耗法力**，每次实际发动按条目支付对应代价"
            "（全力／减速／疯狂为【异变5X】，必中为【异变X】）；"
            "与转化道纹互为残韵双向路径（转化道纹可反向变回原始道纹），"
            "人类与怪物均可经残韵永久获得；"
            "怪物另可经【原初X】临时借用（怪物困境时，借一种自身未持有的原始道纹）。",
            "怪物重复发动同一道纹按同一X计费，不存在「发动越多X越大」的递增。"
            "怪物使用冷却代价类的道纹（固执、束缚、全速）"
            "X 值只能≤1，一律按 X=1 结算（规则正文·怪物准则·共用面板）。",
        ])
A("分支结构（原始 ⇄ 转化，残韵双向）：")
A("")
for src in sorted(ORIGINAL_MONSTER_DAOWEN):
    ds = out_edges.get(src, [])
    A(f"- {src}：{'、'.join(ds) if ds else '—'}")
A("")

section(f"怪物转化道纹（{len(MONSTER_TRANSFORM_DAOWEN)}）", sorted(MONSTER_TRANSFORM_DAOWEN),
        note_lines=["转化道纹与原始怪物道纹互为残韵双向路径：对持有原始道纹的角色发动残韵，"
                    "该道纹永久变为转化道纹，施法者同时永久获得；对持有转化道纹的角色发动同种残韵，"
                    "亦可把它反向变回原始道纹。转化道纹不可由局外【学习】直接习得，只能经此路径取得。"])

for region in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"):
    ns = region_daowen_order(region)
    tier = REGION_TIERS[region]
    section(f"{region}专属（{len(ns)}）", list(ns),
            note_lines=[f"副本阶级：{'一二三四'[tier-1]}阶。学习门禁：先经残韵从本副本怪物处"
                        f"转化获得至少一种本副本道纹，此后才可学习本副本其它专属道纹；其它副本专属道纹不可学习。"],
            loop=f"{region}闭环", loop_title=region)

section(f"初拥专属（{len(FIRST_EMBRACE_DAOWEN)}）", sorted(FIRST_EMBRACE_DAOWEN),
        note_lines=["初拥之夜专属道纹：仅由对应初拥选项授予，不可通过局外学习、【典当】发现或残韵获得。"])

section("未实现（荒疫古城·1）", list(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN),
        note_lines=["所属副本**未接入运行时**（规则草案，见 `副本/荒疫古城.md`）：计算已实现并注册，"
                    "但局外学习按专属道纹拒绝，不进入当前事件池/怪物池。荒疫古城草案闭环中还含"
                    "感染/出土/篡改/尘封/催化/重演/原初等草案道纹，均不在当前版本引擎内。"])

A("---")
A("")
A("## 道纹归属与学习规则")
A("")
A("1. **杀伐闭环（通用核心）**：开局发现初始道纹的来源；人类侧基础概念。")
A("2. **副本专属**：学习门槛=先经残韵从本副本怪物转化获得至少一种；其它副本专属不可学。")
A("3. **怪物转化**：只能由自身已持有道纹经残韵变化获得（施法者同时获得）；原始⇄转化双向，反向可把转化道纹变回原始道纹。")
A("4. **原始怪物**：可经残韵（含转化道纹反向）永久获得，局外【学习】亦可习得；怪物发动按条目支付代价（全力／减速／疯狂异变5X、必中异变X，冷却类X≤1）；可经【原初X】临时借用。")
A("5. **初拥专属**：【真理】仅由初拥之夜选项5授予，不可通过局外学习、【典当】发现或残韵获得。")
A("6. **角色道纹唯一**：同名道纹不重复存在；通过残韵获得的道纹X按自由规则（自由控X）自定义。")
A("7. **道纹默认局内发动**；局外发动须条文明示，当前明示项为【消灾】与【真理】。")
A("8. **自由控X（自由规则的下属条文，不单独成规）**：发动时可自由指定 1 ≤ X ≤ 当前可用法力/代价上限；【波及】【血契】的X还受合法目标数封顶（玩家schema上限按当前存活目标数封顶）。")
A("")

out = ROOT / "全道纹索引.md"
wanted = "\n".join(L) + "\n"

if "--check" in sys.argv:
    # 与 gen_rules.py / gen_ai_context.py 同口径：只校验不落盘，供 CI 与收尾自检调用。
    if not out.exists():
        print(f"MISSING: 缺少 {out.name}，请跑 python3 sim/gen_daowen_index.py")
        raise SystemExit(1)
    if out.read_text(encoding="utf-8") != wanted:
        print("DRIFT: 全道纹索引.md 与引擎/事实源不一致，请跑 python3 sim/gen_daowen_index.py")
        raise SystemExit(1)
    print(f"OK: 全道纹索引.md 与引擎一致（{len(L)} lines, {len(DaoWenEngine._registry)} daowen）")
    raise SystemExit(0)

out.write_text(wanted, encoding="utf-8")
print(f"written {out}: {len(L)} lines, {len(DaoWenEngine._registry)} daowen")
