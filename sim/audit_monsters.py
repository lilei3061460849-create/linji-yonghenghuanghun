#!/usr/bin/env python3
"""
怪物面板合规审计工具（只读，不修改任何文件）
现行唯一口径（2026-09-11：预算并入属性点，规则正文）：
  轮回者同口径：1属性点=6血限，2属性点=1速限=1法限；怪物攻次/攻力分别映射速限/法限，一阶预算25，道纹单独配额。
= panel_cost = ⌈血限/6⌉ + 2×攻击次数 + 2×攻击力 ≤ 25。
（2026-09-14 曾压到35点；2026-09-15 用户令改回60并**原样恢复60点时代的36只面板**，
  攻次/攻力回到 4×4、3×7、1×12 等；9 只单段高攻怪在现行统一计价下超1分，
  血限各 -6 以合规（210→204／198→192／222→216／258→252／270→264）。）
道纹审查：数量=3 / 同池组合唯一 / 池许可（通用核心+原始+转化+本副本专属）
（**道纹总值配额已废止**，2026-09-16 用户令；面板亦不再写死 X，X 由 AI 发动时自选）。
事件/雇佣面板（追求者/医生等）经裁定⑤豁免属性点约束，仅列参照数值。
用法: python sim/audit_monsters.py
"""
import sys, os, math, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
_spec = importlib.util.spec_from_file_location("bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_sim.py"))
bs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bs)

# 道纹池（正文：道纹体系两闭环=通用核心14；道纹归属规则=原始7+转化19；各副本专属8）
CORE = {"杀伐","再生","庇护","固执","连击","波及","增殖","透支","贯穿","束缚","封印"}
ORIGINAL = {"狂暴","全力","疯狂","减速","必中","自愈","飞行"}
# 2026-10-11 用户令（A14）：【无神】【借力】【衰败】已删除；早先批次已删的
# 兴奋/眩晕/滋养/滑翔/坠落 一并不再列入（留着会让审计把已删道纹当成合法面板词条）。
TRANSFORM = {"愤怒","弱化","自食","无力","全速","急速","加速",
             "洞察","蒙蔽","寄生"}
REGION_EXCLUSIVE = {
    "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
    "罪孽都市": {"失忆","典当","豪夺","清算","赎金","假钞","消灾"},   # 2026-10-13 删【逼债】→【典当】顶替
    "龙心谷":   {"加害","龙鳞","逆鳞","裂变","背负","伤痕"},   # 2026-10-11 删【嫁祸】
}

def panel_cost(hp, ap, ac, hp_div):
    return math.ceil(hp / hp_div) + 2 * ac + 2 * ap

def audit():
    monsters = [m for m in bs.parse_monsters() if m.get("region") in REGION_EXCLUSIVE]
    assert len(monsters) == 36, f"应解析36只一阶池怪，实{len(monsters)}"
    ATTR_CAP = 25
    print(f"解析到36只一阶副本池怪，属性点上限={ATTR_CAP}（⌈血限/6⌉+2×攻击次数+2×攻击力）\n")
    print(f"{'怪物':<8}{'副本':<6}{'面板':<13}{'道纹(数量)':<22}"
          f"{'成本':<6}{'判定':<8}{'道纹审查'}")
    all_viol = []
    for region in ["扭曲都市", "罪孽都市", "龙心谷"]:
        seen = {}
        for m in [x for x in monsters if x["region"] == region]:
            cost = panel_cost(m["hp"], m["ap"], m["ac"], 6)
            verdict = "合规" if cost <= ATTR_CAP else f"超{cost-ATTR_CAP}"
            if cost > ATTR_CAP: all_viol.append(m["name"])
            # 道纹审查
            dws = list(m["dw"].items())
            n = len(dws)
            issues = []

            # 2026-09-16 用户令：「道纹总值」配额已废止（它本是"怪物不支付法力"的
            # 补丁，改付法力后由[法限]预算承担约束）。此处不再审查总值，
            # 且面板已不写 X，dw 的值为 None，求和会直接抛 TypeError。
            if n != 3: issues.append(f"数量{n}≠3")
            key = (m["ac"], m["ap"], m["hp"], tuple(sorted(dws)))
            if key in seen: issues.append(f"与{seen[key]}组合重复")
            seen[key] = m["name"]
            legal = CORE | ORIGINAL | TRANSFORM | REGION_EXCLUSIVE[region]
            for d in m["dw"]:
                if d not in legal:
                    src = next((r for r, s in REGION_EXCLUSIVE.items() if d in s and r != region), None)
                    issues.append(f"【{d}】" + (f"系{src}专属" if src else "不在任何许可池"))
            # X 为 None 表示面板未写死、发动时自选，打印时只显示道纹名
            dw_str = "+".join(f"{d}{'' if v is None else v}" for d, v in dws)
            print(f"{m['name']:<8}{region:<6}{m['ac']}×{m['ap']}/{m['hp']:<7}"
                  f"{dw_str+' ('+str(n)+'条)':<22}"
                  f"{cost:<6}{verdict:<8}{'；'.join(issues) if issues else '合规'}")
        print()
    print("===== 汇总 =====")
    print(f"面板违规 {len(all_viol)}/36：{all_viol if all_viol else '无，全部合规'}")

    # 事件/雇佣面板（裁定⑤：豁免属性点约束，仅列参照数值）
    print("\n===== 事件/雇佣面板（裁定⑤豁免属性点，仅参照） =====")
    extra = [
        ("追求者(事件怪/员工)", 8, 2, 96, "逆鳞+活血+固执"),
        ("医生(员工)", 1, 1, 50, "无"),
        ("乞丐(朋友)", 2, 3, 50, "狂暴"),
        ("岩行者(朋友)", 2, 4, 54, "背负"),
        ("赴火者(朋友)", 3, 3, 60, "逆鳞"),
    ]
    for name, ac, ap, hp, dw in extra:
        print(f"{name:<14}{ac}×{ap}/{hp:<6}参照成本={panel_cost(hp,ap,ac,6)} 道纹:{dw}")

if __name__ == "__main__":
    audit()
