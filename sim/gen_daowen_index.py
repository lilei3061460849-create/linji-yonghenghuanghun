#!/usr/bin/env python3
"""生成 全道纹索引.md（一次性工具：道纹数据源变更后重跑即可）。

数据源（全部取当前版本引擎/文档事实，不抄旧数据）：
- 效果正文/支付方式：运行时道纹取 engine/rule_engine.py 或 DaoWenEngine.calculate_*；DRAFT_DAOWEN 仅预留给未来的纯草案节点
- 归属分类：engine/gamedata.py（通用核心 / ORIGINAL / TRANSFORM / REGION_EXCLUSIVE / RACE_EXCLUSIVE / BLOODLINE_EXCLUSIVE / UNIMPLEMENTED）
- 残韵闭环：engine/daowen.py DaoWenEngine.CLOSED_LOOPS
- 承载怪物：已实现副本文档及沉沦海专属网络草案的全部面板行（怪物池 + 事件/雇佣面板）
"""
import inspect
import difflib
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.daowen import DaoWenEngine, ResonanceEngine, display_resonance_node  # noqa: E402
from engine import derivatives as derivative_rules  # noqa: E402
from engine.rule_engine import DAOWEN_RULES, UNMIGRATED_REASON  # noqa: E402
from engine.gamedata import (  # noqa: E402
    MONSTER_TRANSFORM_DAOWEN,
    ORIGINAL_MONSTER_DAOWEN,
    REGION_EXCLUSIVE_DAOWEN,
    REGION_TIERS,
    SHAFA_LOOP_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN,
    UNIMPLEMENTED_DAOWEN,
    FIRST_EMBRACE_DAOWEN,
    RACE_EXCLUSIVE_DAOWEN,
    BLOODLINE_EXCLUSIVE_DAOWEN,
)
from engine.dungeons import load_dungeon_documents  # noqa: E402

DaoWenEngine.register_all()

# 纯草案节点只收录定义，不进入DaoWenEngine运行时注册表。
# 流动与遗忘已于2026-10-29接入运行时，改从DaoWenEngine docstring取正本。
# 2026-11-02 用户令：龙心谷【炽热】【熔化】【锻造】已实装，草案字典清空。
# 这里只预留给未来的纯草案节点（未注册进 DaoWenEngine 的候选），实装后必须移出。
DRAFT_DAOWEN = {}

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

for name, draft in DRAFT_DAOWEN.items():
    costs[name] = draft["cost"]
    effects[name] = draft["effect"]
    params_of[name] = draft["params"]

# 承载怪物：扫描已实现副本 + 本阶段纳入道纹索引的沉沦海草案面板。
# 其它未实现草案仍不计入当前可用道纹的承载怪物列。
carriers = defaultdict(list)
dungeon_documents = load_dungeon_documents()
sunken_sea_path = ROOT / "副本" / "沉沦海.md"
if sunken_sea_path.is_file():
    dungeon_documents["沉沦海"] = sunken_sea_path.read_text(encoding="utf-8")
for region, text in sorted(dungeon_documents.items()):
    for line in text.splitlines():
        m = PANEL.match(line.strip())
        if m and m.group(6):
            # 新面板格式的道纹列不带次数（如「畸变，坏死，狂暴」），旧格式带次数（如「执念2」）。
            # 两种都解析：有次数就带次数，没有就只记怪物名。
            for token in re.split(r'[，,、]', m.group(6)):
                token = token.strip()
                mm = re.match(r'^([\u4e00-\u9fff·]+?)(\d+)?$', token)
                if not mm or mm.group(1) not in (set(DaoWenEngine._registry) | set(DRAFT_DAOWEN)):
                    continue
                entry = f"{m.group(1)}{mm.group(2) or ''}"
                if entry not in carriers[mm.group(1)]:
                    carriers[mm.group(1)].append(entry)

# 残韵边（双向：2026-10-07 用户令——同一条边正反两向都走，同一种残韵、同样消耗一次）
out_edges = defaultdict(list)
for _name in (list(DaoWenEngine._registry) + list(DRAFT_DAOWEN)):
    for _p in ResonanceEngine.get_available_resonance(_name):
        out_edges[_name].append(
            f"（{_p['resonance_type']}）→{_p['target_daowen']}"
            f"（{'正向' if _p['direction'] == '正向' else '反向'}）")
in_edges = defaultdict(list)

# 目标需求
def target_label(name):
    p = params_of[name]
    if name == "波及":
        return "X个（显式提交）"
    if name == "共罪":
        return "X个非自身（显式提交）"
    if name == "超频":
        return "需显式选定"
    if name == "唤潮":
        return "自身周围X个可用地图格"
    if name == "蜃景":
        return "自身所在格"
    if "target" in p:
        return "需显式选定"
    # 【必中】的结算函数签名是 calculate_bizhong(x)，没有 target 形参，会被下面的
    # 兜底判成「自身」——那是 2026-09-28 的 self-buff 旧口径。2026-10-12 用户令增强后
    # 它是「你选中的[目标]无法进行回合外行动」，按规则文必须显式选定目标。
    if name == "必中":
        return "需显式选定"
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
    # 纯草案节点（未注册进运行时的候选）也归入其副本小节，与可学习/可发动的专属池分开标记。
    ns = set(REGION_EXCLUSIVE_DAOWEN[region]) | {
        name for name, owner in UNIMPLEMENTED_DAOWEN.items()
        if owner == region and name in DRAFT_DAOWEN
    }
    # 2026-11-02 四面体大改：顺序取该副本四面体里的节点出场序（已去重）。
    chain: list = [n for n in ResonanceEngine.nodes_of_region(region) if n in ns]
    return chain + sorted(ns - set(chain))


# 分类
CATEGORY = {}
for n in SHAFA_LOOP_DAOWEN:
    CATEGORY[n] = "shaifa"
CATEGORY["共罪"] = "unimpl"
CATEGORY["怜悯"] = "unimpl"      # 2026-11-02：怜悯随永夜庭四面体移入永夜庭
# 2026-11-02：【操纵】【迟滞】随通用四面体二进通用核心；【透支】出生自带、【篡改】
# 由荒疫古城移入通用，二者不属于杀伐发现池，列「其他通用」。
# 2026-11-07 曾互换【封印】/【篡改】的四面体位置；2026-11-08 用户令再次调换：
# 【封印】回四面体二（随 SHAFA_LOOP_DAOWEN 标 shaifa），【篡改】回四面体四，列「其他通用」。
CATEGORY["透支"] = "other"
CATEGORY["篡改"] = "other"
# 2026-11-09：衍生道纹不属任何四面体，单列一节；事实源是 engine/derivatives.py。
for _n in derivative_rules.DERIVED_DAOWEN:
    CATEGORY[_n] = "derivative"
for n in ORIGINAL_MONSTER_DAOWEN:
    CATEGORY[n] = "original"
for n in MONSTER_TRANSFORM_DAOWEN:
    CATEGORY[n] = "transform"
for region in REGION_EXCLUSIVE_DAOWEN:
    for n in region_daowen_order(region):
        CATEGORY[n] = region
for race, pool in RACE_EXCLUSIVE_DAOWEN.items():
    for n in pool:
        CATEGORY[n] = race
for n in BLOODLINE_EXCLUSIVE_DAOWEN:
    CATEGORY[n] = "bloodline"
for n in FIRST_EMBRACE_DAOWEN:
    CATEGORY[n] = "first_embrace"
for n, region in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.items():
    CATEGORY[n] = "unimpl"

# 「其他通用」按显式顺序渲染（不属于通用四面体、也不属于任何副本四面体的通用道纹）。
OTHER_DAOWEN = ["透支", "篡改"]

assert set(CATEGORY) == (set(DaoWenEngine._registry) | set(DRAFT_DAOWEN)), (
    set(CATEGORY) ^ (set(DaoWenEngine._registry) | set(DRAFT_DAOWEN)))

# 特殊注记（引擎机制，非数值）
NOTES = {
    "血契": "按你的名义支付量让目标额外支付X倍同种资源，不是代付；目标资源不足扣至0，生命不足正常命零。血契不能局外生效。",
    "萎缩": "只禁止获得速度，不削减速限，也不阻止失去或支付速度；与超频是同域反转，既有增殖边保留为显式分叉。",
    "唯一": "任何方式成功使用过的道纹都进入历史；持续X内不能再次使用。",
    "封印": "玩家支付异变X，使一个显式选定的目标怪物延后X回合再入场；暂离仍阻塞战终，回场当回合沿用增援白板，最终命零正常进入死亡与碎片结算。",
    "分裂": "每次发动创造X个独立造物；三项依次为1血限/当前生命、1速限/当前速度、1法限/当前法力，按通用规则得到1击数与1攻击力；与所有角色共用基础1次出手及通用修正，不另设专用上限，无道纹、不继承施法者构筑，[战终]消散。",
    "凋零": "一次性瞬发效果，只在施放当次按场上快照判定；不挂持续状态，后续生命变化及新单位入场不会再次触发。只判定除[目标]外其他在场角色的当前生命，施法者并非目标时同样参与判定；直接判死而非伤害。",
    "压迫": "纯目标限制：除本次显式[目标]外，所有角色仅能显式选择自身为[目标]，持续X回合；状态挂在该目标身上，不检查生命、不判死。与飞行互为反转。",
    "沉溺": "目标即将进入普通回合或大梦产生的额外回合时消耗1次阻断并跳过该次完整回合；降至0的本次仍跳过。被跳过者不执行回始/回终、主动出手、凡庸计数或状态递减，其他角色与世界回合照常。",
    "招魂": "唤回者为[造物]，[战终]消失。",
    "真理": "初拥之夜选项5授予的血族专属道纹。X=陈述句数：提出X句陈述，目标须逐句言明真理，任一句无法言明则无法开口，持续X回合；真伪由DM逐句裁定。冷却2场战斗。",
    "原初": "支付异变5X，借用目标身上自身未拥有的随机X种道纹。",
    "透支": "不在任何闭环内：任意角色出生自带，X每次自选。",
    "操纵": "目标接下来X次除施法外的行动由施法者控制；每次执行消耗1次名额，名额用完状态消失。施法者须给出指令（攻击／跳过／使用消耗品），未给指令时到该行动作废、代价不退。",
    "饥饿": "施加时立即把[血限]压到当前生命（不产生失血）；持续X，到期还原施加前的[血限]。",
    "扭曲": "累积治疗量即[癌变]用的累计回复量：×2^X 后立即按癌变阈值复核一次。",
    "淬火": "当前耐久+X，并保证耐久上限不低于结果值（对完好消耗品等价于同时抬上限）。",
    "涌潮": "法限变为∞期间当前法力可突破法限；到期把当前法力压回原法限。与遗物【苍白之花】同域，但可作用于任意目标。",
    "迟滞": "每一次行动所需出手+1，含闪避、招架这类零出手的回合外行动。",
}

def tetra_chain(region):
    """把一个副本的四面体关系渲染成「反转/转换/曲解 各一组 ⇄」的文本。"""
    blocks = []
    for name, edges in ResonanceEngine.tetrahedra_of_region(region).items():
        groups = {}
        for src, rtype, dst in edges:
            groups.setdefault(rtype, []).append(
                f"{display_resonance_node(src)}⇄{display_resonance_node(dst)}")
        rendered = "；".join(f"{rtype}：{'、'.join(pairs)}"
                             for rtype, pairs in groups.items())
        blocks.append(f"{name}（{rendered}）")
    return "　｜　".join(blocks)

# ---------- 渲染 ----------
L = []
A = L.append
A("# 全道纹索引")
A("")
A("查道纹用这份。每条写清：怎么支付、效果是什么、作用在谁身上、和哪些道纹互为残韵、哪些怪物会带。")
A("规则的完整叙述以[规则正文](规则正文.md)为准，这里只做速查。")
A("")
A(f"本索引收录 {len(DaoWenEngine._registry) + len(DRAFT_DAOWEN)} 条道纹："
  f"通用核心（四面体一·二）{len(SHAFA_LOOP_DAOWEN)}、衍生道纹 {len(derivative_rules.DERIVATIVE_SPECS)}、"
  f"原始怪物 {len(ORIGINAL_MONSTER_DAOWEN)}、怪物转化 {len(MONSTER_TRANSFORM_DAOWEN)}、"
  f"副本专属 {sum(len(region_daowen_order(r)) for r in REGION_EXCLUSIVE_DAOWEN)}、"
  f"鲛族 {sum(len(pool) for pool in RACE_EXCLUSIVE_DAOWEN.values())}、"
  f"初拥 {len(FIRST_EMBRACE_DAOWEN)}、"
  f"其他通用 {len(OTHER_DAOWEN)}、副本未开放 {len(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN)}。")
A("")
A("> 通用规则（自由控X、[目标]与闪避、代价结算、平分、声明、怪物冷却道纹X≤1 等）见[规则正文](规则正文.md)，本文件不重复。")
A("")
A("## 目录")
A("")
A("- [总览表](#总览表)")
A(f"- [通用核心（四面体一·二，{len(SHAFA_LOOP_DAOWEN)}）](#通用核心四面体一二{len(SHAFA_LOOP_DAOWEN)})")
A(f"- [其他通用道纹（{len(OTHER_DAOWEN)}）](#其他通用道纹{len(OTHER_DAOWEN)})")
A(f"- [衍生道纹（{len(derivative_rules.DERIVATIVE_SPECS)}）](#衍生道纹{len(derivative_rules.DERIVATIVE_SPECS)})")
A(f"- [原始怪物道纹（{len(ORIGINAL_MONSTER_DAOWEN)}）](#原始怪物道纹{len(ORIGINAL_MONSTER_DAOWEN)})")
A(f"- [怪物转化道纹（{len(MONSTER_TRANSFORM_DAOWEN)}）](#怪物转化道纹{len(MONSTER_TRANSFORM_DAOWEN)})")
for _r in REGION_EXCLUSIVE_DAOWEN:
    A(f"- [{_r}专属（{len(region_daowen_order(_r))}）](#{_r}专属{len(region_daowen_order(_r))})")
for _race, _pool in RACE_EXCLUSIVE_DAOWEN.items():
    A(f"- [{_race}专属（{len(_pool)}）](#{_race}专属{len(_pool)})")
if BLOODLINE_EXCLUSIVE_DAOWEN:
    A(f"- [血族专属（{len(BLOODLINE_EXCLUSIVE_DAOWEN)}）](#血族专属{len(BLOODLINE_EXCLUSIVE_DAOWEN)})")
A(f"- [初拥专属（{len(FIRST_EMBRACE_DAOWEN)}）](#初拥专属{len(FIRST_EMBRACE_DAOWEN)})")
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
    "乱葬岗": "乱葬专属", "沉沦海": "沉沦海专属", "荒疫古城": "荒疫专属", "鲛族": "鲛族专属",
    "bloodline": "血族专属", "other": "其他通用", "first_embrace": "初拥专属", "unimpl": "副本未开放",
    "derivative": "衍生",
}
for name in sorted(DaoWenEngine._registry, key=lambda n: (list(CATEGORY).index(n), n)):
    pass
order = (list(SHAFA_LOOP_DAOWEN) + ["共罪", "怜悯"] + sorted(ORIGINAL_MONSTER_DAOWEN)
         + sorted(MONSTER_TRANSFORM_DAOWEN)
         + [n for r in REGION_EXCLUSIVE_DAOWEN for n in region_daowen_order(r)]
         + [n for pool in RACE_EXCLUSIVE_DAOWEN.values() for n in sorted(pool)]
         + sorted(BLOODLINE_EXCLUSIVE_DAOWEN)
         + sorted(FIRST_EMBRACE_DAOWEN)
         + list(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN)
         + [s.name for s in derivative_rules.DERIVATIVE_SPECS])
for name in order:
    out = "、".join(out_edges[name]) if out_edges[name] else "—"
    if name == "共罪":
        out = "—（不属于残韵闭环）"
    c = carriers.get(name, [])
    cstr = "、".join(c[:6]) + (f"…+{len(c)-6}" if len(c) > 6 else "") or "—"
    A(f"| {name} | {CAT_LABEL[CATEGORY[name]]} | {costs[name]} | {target_label(name)} | {out} | {cstr} |")
A("")
A("> 「…」表示承载怪物超过 6 种，逐条见下方分类小节。")
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
        A(f"残韵关系（四面体，双向）：{tetra_chain(loop)}")
        A("")
    for name in names:
        A(f"### {name}")
        # 条目正文必须以道纹名开头：旧模板漏了名字，只渲染成「X：支付：…」。
        A(f"{name}X：{costs[name]}。{effects[name]}" if effects[name]
          else f"{name}X：{costs[name]}。")
        meta = [f"[目标]：{target_label(name)}"]
        if name in DRAFT_DAOWEN:
            meta.append("执行状态：当前定义尚未实现")
        if name == "共罪":
            meta.append("残韵路径：—（不属于残韵闭环）")
        elif out_edges[name]:
            meta.append("残韵路径（双向）：" + "、".join(out_edges[name]))
        c = carriers.get(name, [])
        meta.append("承载：" + ("、".join(c) if c else "—"))
        A(f"> {' ｜ '.join(meta)}")
        if name in NOTES:
            A(f"> 注：{NOTES[name]}")
        A("")

section(f"通用核心（四面体一·二，{len(SHAFA_LOOP_DAOWEN)}）", list(SHAFA_LOOP_DAOWEN),
        note_lines=["开局【发现】初始道纹只从本闭环抽（随机列出3种未持有候选，显式选1；杀伐不是默认起手）。",
                    "通用核心道纹为人类侧基础概念，局外可经学习/残韵获得。"],
        loop="通用")

section(f"其他通用道纹（{len(OTHER_DAOWEN)}）", OTHER_DAOWEN,
        note_lines=["不属于通用四面体一·二（发现池），也不属于任何副本专属池。",
                    "【透支】不在任何四面体内：任意角色出生自带，X每次自选；",
                    "【封印】属通用，关系见通用四面体四。"])

DERIVATIVE_NAMES = [s.name for s in derivative_rules.DERIVATIVE_SPECS]
for spec in derivative_rules.DERIVATIVE_SPECS:
    if spec.name not in NOTES:
        NOTES[spec.name] = f"【{spec.parent}】衍生：{spec.note}"
section(f"衍生道纹（{len(DERIVATIVE_NAMES)}）", DERIVATIVE_NAMES,
        note_lines=[
            "公式：母本道纹→支付【转换】×1＋血限2＋灵魂碎片3→从该母本的衍生道纹池中【发现】一种衍生道纹获取。",
            "每个衍生道纹独立占用 1 张死者之页；对本次结果不满意可重新支付血限2刷新选项。",
            "衍生道纹严格受限于母本道纹的法则领域，不可跨界产生其他道纹的法则效果；其本质是法则内部的收益置换——改变支付途径，或牺牲灵活性、安全性、适用性来换更高数值汇率或特殊机制。",
            "只能通过衍生发现获取：不进学习／典当／开局发现池，不占残韵四面体节点；衍生道纹自身不能再作母本。完整口径见[规则正文](规则正文.md#衍生道纹)。",
            "多途径道纹一律只留一条途径：母本【杀伐】法力X→生命-X²、【透支】枯竭X→X法力、【再生】法力X→回复4X生命；其余同效果途径全部列为衍生道纹。",
        ])

section(f"原始怪物道纹（{len(ORIGINAL_MONSTER_DAOWEN)}）", sorted(ORIGINAL_MONSTER_DAOWEN),
        note_lines=[
            "原始怪物道纹是各转化分支的起点：**不消耗法力**，每次实际发动按条目支付对应代价"
            "（全力为【异变5X】，必中为【异变X】；减速／疯狂已改为遗物）；"
            "与转化道纹互为残韵双向路径（转化道纹可反向变回原始道纹），"
            "人类与怪物均可经残韵永久获得；"
            "【原初X】随机借用目标身上自身未拥有的X种道纹，不限原始怪物道纹池。",
            "怪物重复发动同一道纹按同一X计费，不存在「发动越多X越大」的递增。"
            "怪物使用冷却代价类的道纹（束缚、全速）"
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
A("> 原转化道纹【愤怒】已转换为消耗品【怒火针】，固定使指定目标法力消耗减半，持续2回合；详见[物品索引](物品索引.md#怒火针)。")
A("")

for region in REGION_EXCLUSIVE_DAOWEN:
    ns = region_daowen_order(region)
    tier = REGION_TIERS[region]
    notes = [f"副本阶级：{'一二三四'[tier-1]}阶。学习门禁：先经残韵从本副本怪物处转化获得至少一种本副本道纹，此后才可学习本副本其它专属道纹；其它副本专属道纹不可学习。"]
    if region == "沉沦海":
        notes.append("【唤潮】【蜃景】不属于闭环，由鲛渊陵墓的两个独立选项获得；怪物面板既有配置保留。")
        notes.append("【流动】【遗忘】须按X选择持续状态，流动还要另选接收目标；可选状态数量不足时拒绝发动。")
        notes.append("【涌潮】与【冰冻】互为反转，效果与遗物【苍白之花】同域。")
    if region == "龙心谷":
        notes.append("龙心谷满 8 纹、两组四面体（2026-11-08 最终结构）：四面体一=淬火/炽热/百炼/锈蚀，四面体二=锻造/熔化/回炉/重铸。")
        notes.append("【锻造】只发现既有固定规格消耗品，不接受自定义物品名称或效果；领取时耐久按本次 X 覆盖。")
    section(f"{region}专属（{len(ns)}）", list(ns), note_lines=notes,
            loop=region, loop_title=region)
    if region == "龙心谷":
        A("> 原龙心谷专属道纹【加害】已正式转换为消耗品【灼罪钉】，固定使指定目标每次受到伤害+1，持续2回合；详见[物品索引](物品索引.md#灼罪钉)。")
        A("")

for race, pool in RACE_EXCLUSIVE_DAOWEN.items():
    section(f"{race}专属（{len(pool)}）", sorted(pool),
            note_lines=[f"仅通过{race}专属机制获得；不计入任何副本专属学习池。"])

if BLOODLINE_EXCLUSIVE_DAOWEN:
    section(f"血族专属（{len(BLOODLINE_EXCLUSIVE_DAOWEN)}）", sorted(BLOODLINE_EXCLUSIVE_DAOWEN),
            note_lines=["仅通过血族专属机制获得；不计入任何副本专属学习池。",
                        "血族专属获取机制尚未接入；当前不可通过局外学习、【典当】发现或残韵获得。"])
else:
    A("> 原血族专属道纹【消灾】已改为消耗品【逆命筹】，详见[物品索引](物品索引.md#逆命筹)。")

section(f"初拥专属（{len(FIRST_EMBRACE_DAOWEN)}）", sorted(FIRST_EMBRACE_DAOWEN),
        note_lines=["初拥之夜专属道纹：仅由对应初拥选项授予，不可通过局外学习、【典当】发现或残韵获得。"])

for owner in sorted(set(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.values())):
    names = [name for name, region in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.items() if region == owner]
    section(f"{owner}专属道纹（{len(names)}）", names,
            note_lines=["所属副本尚未开放：这些道纹的条文已定，但暂时无法学习或获得。"])

A("---")
A("")
A("## 道纹归属与学习规则")
A("")
A("1. **通用核心（四面体一·二）**：开局发现初始道纹的来源；人类侧基础概念。")
A("2. **副本专属**：学习门槛=先经残韵从本副本怪物转化获得至少一种；其它副本专属不可学。")
A("3. **怪物转化**：只能由自身已持有道纹经残韵变化获得（施法者同时获得）；原始⇄转化双向，反向可把转化道纹变回原始道纹。")
A("4. **原始怪物**：可经残韵（含转化道纹反向）永久获得，局外【学习】亦可习得；怪物发动按条目支付代价（全力异变5X、必中异变X；减速／疯狂已是遗物，冷却类X≤1）；可作为【原初X】的借用对象，但原初不再仅限原始怪物道纹池。")
A("5. **血族专属转换消耗品**：【逆命筹】当前尚无专属获取机制。")
A("6. **初拥专属**：【真理】仅由初拥之夜选项5授予，不可通过局外学习、【典当】发现或残韵获得。")
A("7. **角色道纹唯一**：同名道纹不重复存在；通过残韵获得的道纹X按自由规则（自由控X）自定义。")
A("8. **道纹默认局内发动**；局外发动须条文明示，当前明示项为【真理】【唤潮】【蜃景】；【唤潮】【蜃景】仅作用于开放地图。")
A("9. **自由控X（自由规则的下属条文，不单独成规）**：发动时可自由指定 1 ≤ X ≤ 当前可用法力/代价上限；【波及】的X还受合法目标数封顶；【共罪】的目标数随X（X个非自身目标），持续∞。")
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
        diff = difflib.unified_diff(
            out.read_text(encoding="utf-8").splitlines(),
            wanted.splitlines(),
            fromfile="全道纹索引.md",
            tofile="sim/gen_daowen_index.py",
            lineterm="",
        )
        print("\n".join(diff))
        raise SystemExit(1)
    print(f"OK: 全道纹索引.md 与引擎一致（{len(L)} lines, {len(DaoWenEngine._registry) + len(DRAFT_DAOWEN)} indexed daowen; {len(DaoWenEngine._registry)} runtime）")
    raise SystemExit(0)

out.write_text(wanted, encoding="utf-8")
print(f"written {out}: {len(L)} lines, {len(DaoWenEngine._registry) + len(DRAFT_DAOWEN)} indexed daowen; {len(DaoWenEngine._registry)} runtime")
