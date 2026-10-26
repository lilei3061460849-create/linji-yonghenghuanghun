"""
战报格式化器：严格按 推演规范《六、战斗推演格式》输出。

规范原文（推演规范《六、战斗推演格式》）要求的结构：

    [战始]
    出怪：范围与随机数结果→本场敌人清单
    战斗背景：（名称与影响）
    敌方面板：名称（攻击次数×攻击力/[血限]，道纹）
    我方面板：轮回者（[血限]/[法限]/[速限]，出手次数）｜[朋友]与[员工]（攻击次数×攻击力/[血限]，道纹）
    [战始]效果结算：（遗物、法器、法术开启，逐条列出）

    第N回合
    [回始]：资源面板（双方当前生命/[血限]、当前法力/[法限]、当前速度/[速限]、格挡、持续X剩余回合）→[回始]类效果逐条结算
    出手1（行动者）：发动内容→消耗或代价→目标响应→伤害与效果结算→双方数值变化
    出手2（行动者）：同上
    [回终]：[回终]类效果结算→格挡清空→持续X剩余回合-1→本回合资源面板

    [战终]
    死亡结算→[碎片]奖励计算→增益与减益清除→代价保留项→[朋友][员工]留存与[临时朋友]消失→精力恢复

设计约束（对应 推演铁律）：
1. 本模块只做"排版"，不产生任何数值。所有数字均取自引擎返回的结果字典 /
   GameState 实体字段。禁止在此处推算、估算或补写引擎没有给出的数值。
2. 每一次出手逐条列出，禁止概括、跳过或合并结算（铁律·格式段首句）。
3. 出手行必须写明消耗或代价的具体数值与资源变化前后的实际数字（铁律1）。
4. 闪避必须显式书写（铁律5）。
"""
from __future__ import annotations

from typing import Any, Optional


def _daowen_str(entity: Any) -> str:
    """把实体的道纹渲染成 '名称X' 形式，例如 背负3。x_value 缺失时只写名称。"""
    parts = []
    for name, inst in getattr(entity, "dao_wen", {}).items():
        x = getattr(inst, "x_value", None)
        parts.append(f"{name}{x}" if x else f"{name}")
    return "、".join(parts) if parts else "无"


def _lethal_progress(entity: Any) -> list[str]:
    """致死类特殊事件的进度串（迷失／癌变／凡庸），例如 ['迷失（10/50）']。

    2026-09-15 用户令：致死事件的进度必须随面板一起输出，AI 才看得见自己离
    【迷失】这类命零还有多远，不会"自爆"。口径唯一事实源＝Entity.lethal_progress()。
    """
    fn = getattr(entity, "lethal_progress", None)
    return list(fn()) if callable(fn) else []


def _status_str(entity: Any) -> str:
    """渲染 持续X 剩余回合，规范要求资源面板包含该项。"""
    out = []
    for st in getattr(entity, "status_effects", []):
        nm = getattr(st, "name", None) or getattr(st, "status_type", "?")
        rd = getattr(st, "remaining_rounds", None)
        if rd is None:
            rd = getattr(st, "duration", None)
        out.append(f"{nm}{'(持续' + str(rd) + ')' if rd not in (None, 0) else ''}")
    return "、".join(out) if out else "无"


def enemy_panel(entity: Any) -> str:
    """敌方面板：名称（攻击次数×攻击力/[血限]，道纹）"""
    return (f"{entity.name}（{entity.attack_count}×{entity.attack_power}"
            f"/{entity.blood_limit}，{_daowen_str(entity)}）")


def ally_panel(entity: Any) -> str:
    """[朋友]与[员工]：名称（攻击次数×攻击力/[血限]，道纹）"""
    return (f"{entity.name}（{entity.attack_count}×{entity.attack_power}"
            f"/{entity.blood_limit}，{_daowen_str(entity)}）")


def player_panel(player: Any) -> str:
    """我方面板·轮回者（[血限]/[法限]/[速限]，出手次数）"""
    return (f"{player.name}（{player.blood_limit}/{player.mana_limit}"
            f"/{player.speed_limit}，出手{getattr(player, 'action_count', 0)}次）")


def resource_line(entity: Any) -> str:
    """资源面板单行：当前生命/[血限] 当前法力/[法限] 当前速度/[速限] 格挡 持续X"""
    return (f"{entity.name} 生命{entity.current_hp}/{entity.blood_limit}"
            f" 法力{entity.current_mana}/{entity.mana_limit}"
            f" 速度{entity.current_speed}/{entity.speed_limit}"
            + (f" 格挡{entity.shield}" if entity.shield else "")
            + (f" 持续[{_status_str(entity)}]" if entity.status_effects else "")
            + (f" 致死进度[{'、'.join(_lethal_progress(entity))}]" if _lethal_progress(entity) else ""))


def format_setup_discovery(
    *,
    blood_points: int,
    speed_points: int,
    mana_points: int,
    blood_limit: int,
    mana_limit: int,
    speed_limit: int,
    action_count: int,
    shards: int,
    daowen_options: list[str],
    daowen_pick: str,
    resonance: str,
    relic_options: list[str],
    relic_pick: str,
    region: str = "",
) -> list[str]:
    """渲染 [开局]：必须先列出发现候选，再写选择。缺候选直接拒绝。"""
    dao_opts = [str(name) for name in (daowen_options or []) if name]
    relic_opts = [str(name) for name in (relic_options or []) if name]
    if not dao_opts:
        raise ValueError("开局战报必须列出初始道纹发现候选，不能只写选择结果")
    if daowen_pick not in dao_opts:
        raise ValueError("初始道纹选择必须落在本次发现候选中")
    if not relic_opts:
        raise ValueError("开局战报必须列出遗物发现候选，不能只写选择结果")
    if relic_pick not in relic_opts:
        raise ValueError("遗物选择必须落在本次发现候选中")
    lines = [
        "[开局]",
        (f"属性：{blood_points}血/{speed_points}速/{mana_points}法"
         f"＝{blood_limit}/{mana_limit}/{speed_limit}，出手{action_count}｜碎片{shards}"),
        f"遗物发现：候选〔{'、'.join(relic_opts)}〕→选择【{relic_pick}】",
        f"初始道纹发现：候选〔{'、'.join(dao_opts)}〕→选择【{daowen_pick}】",
        f"残韵：{resonance}",
    ]
    if region:
        lines.append(f"副本：{region}")
    return lines


def format_battle_start(
    *,
    battle_no: int,
    draw_range: str,
    draw_result: str,
    enemies: list,
    player: Any,
    allies: Optional[list] = None,
    background: str = "",
    # 战斗背景仅作为【许愿】等特殊事件的场景素材，本身不带数值影响。
    # 未实际触发特殊事件时不再逐场重复"纯叙事，不影响数值"这句注解。
    background_effect: str = "",
    start_effects: Optional[list] = None,
) -> list[str]:
    """渲染 [战始] 段。start_effects 逐条列出遗物/法器/法术开启。"""
    allies = allies or []
    lines = [f"[战始]（第{battle_no}场）"]
    lines.append(f"出怪：{draw_range}→{draw_result}")
    lines.append(f"战斗背景：{background}"
                 + (f"（{background_effect}）" if background_effect else ""))
    for e in enemies:
        lines.append(f"敌方面板：{enemy_panel(e)}")
    ally_txt = "｜".join(ally_panel(a) for a in allies) if allies else "无[朋友]与[员工]"
    lines.append(f"我方面板：{player_panel(player)}｜{ally_txt}")
    lines.append("[战始]效果结算：")
    if start_effects:
        for i, eff in enumerate(start_effects, 1):
            lines.append(f"  {i}. {eff}")
    else:
        lines.append("  无")
    return lines


def format_round_start(round_no: int, rs_result: dict, player: Any, enemies: list) -> list[str]:
    """渲染 第N回合 + [回始] 资源面板与逐条效果。数值全部来自引擎。"""
    lines = [f"", f"第{round_no}回合"]
    # 资源面板逐行书写：我方一行、每个敌人各一行，避免挤成一长串难以阅读
    lines.append("[回始]：")
    lines.append(f"　我方　{resource_line(player)}")
    for e in enemies:
        if e.is_alive:
            lines.append(f"　敌方　{resource_line(e)}")
    effects = (rs_result or {}).get("effects", []) or []
    if effects:
        for eff in effects:
            lines.append(f"  → {_render_effect(eff)}")
    else:
        lines.append("  → 无[回始]类效果")
    return lines


def _render_effect(eff: dict) -> str:
    """把引擎的 effect 字典转成文字，不添加引擎未给出的数值。"""
    t = eff.get("type", "")
    # DM裁定 2026-09-09：法力改一池制（[战始]给满、[回始]不回填、[敌回终]不清空），
    # mana_refill / mana_clear 两种条目已无产出，对应渲染分支一并删除（不留死接线）。
    if t == "shield_clear":
        return f"{eff.get('entity')} 格挡清空：清除{eff.get('cleared')}点"
    if t == "damage":
        return (f"{eff.get('target')} 受到伤害{eff.get('actual_damage')}"
                f"（格挡吸收{eff.get('shield_absorbed')}）"
                f"，生命{eff.get('hp_before')}→{eff.get('hp_after')}")
    if t == "heal":
        who = eff.get("entity") or eff.get("target")
        amt = eff.get("actual_heal", eff.get("amount"))
        before, after = eff.get("hp_before"), eff.get("hp_after")
        if before is not None and after is not None:
            return f"{who} [回复]{amt}点生命（生命由{before}升至{after}）"
        return f"{who} [回复]{amt}点生命"
    if t == "heal_pct":
        who = eff.get("entity") or eff.get("target")
        amt = eff.get("actual_heal", eff.get("amount"))
        before, after = eff.get("hp_before"), eff.get("hp_after")
        if before is not None and after is not None:
            return f"{who} [回复]{amt}点生命（百分比，生命由{before}升至{after}）"
        return f"{who} [回复]{amt}点生命（百分比）"
    if t == "aoe_damage":
        return (f"{eff.get('target')} 受到范围伤害{eff.get('actual_damage')}"
                f"（格挡吸收{eff.get('shield_absorbed', 0)}）"
                f"，生命{eff.get('hp_before')}→{eff.get('hp_after')}")
    if t == "shield":
        return f"{eff.get('target') or eff.get('entity')} 获得格挡 {eff.get('amount')}"
    if t == "bleed_cost":
        return (f"{eff.get('source') or eff.get('entity')} 支付【流血】代价 "
                f"{eff.get('actual_damage')}，生命{eff.get('hp_before')}→{eff.get('hp_after')}")
    if t == "aging_cost":
        return f"{eff.get('entity') or eff.get('source')} 支付【衰老】代价 {eff.get('amount')}"
    if t == "mana_gain":
        return f"{eff.get('entity') or eff.get('target')} 获得法力 {eff.get('amount')}"
    if t == "status_added":
        return (f"{eff.get('target') or eff.get('entity')} 获得状态【{eff.get('status')}】"
                f"{eff.get('value', '')}"
                + (f"（持续{eff.get('duration')}）" if eff.get("duration") else ""))
    if t == "blood_limit_reduction":
        return f"{eff.get('target')} [血限]降至 {eff.get('new_blood_limit')}"
    if t == "blood_limit_increase":
        return f"{eff.get('target')} [血限]+{eff.get('increase')}"
    if t == "mediocrity":
        return f"【凡庸】{eff.get('entity')}：{eff.get('note')}"
    if t == "mediocrity_loot":
        return f"　{eff.get('note')}"
    if t == "deform_damage":
        return (f"{eff.get('entity')} 受【畸变】结算，失去 {eff.get('blood_loss')} 生命"
                f"（剩余{eff.get('hp_after')}）")
    if t == "blood_lineage_heal":
        return f"{eff.get('entity')} 触发【血族血脉】：[回复]{eff.get('amount')}"
    if t == "blood_lineage_bleed":
        return f"{eff.get('entity')} 触发【血族血脉】：流血{eff.get('amount')}"
    if t == "seal":
        return (f"{eff.get('target')} 被【封印】暂离，延后{eff.get('delay_rounds')}回合"
                f"于第{eff.get('return_round')}回合始再入场")
    if t == "seal_reentry":
        return f"{eff.get('entity')}结束【封印】暂离，于第{eff.get('round')}回合始重新入场"
    if t == "speed_boost":
        return f"{eff.get('entity') or eff.get('target')} 速度+{eff.get('amount')}"
    if t == "attack_fixed":
        return f"{eff.get('target')} 攻击力被固定为 {eff.get('value', 1)}"
    if t == "relic":
        return f"遗物：{eff.get('log')}"
    if t == "status_expired":
        expired = "、".join(eff.get("expired_effects", []))
        return f"{eff.get('entity') or eff.get('target')} 状态到期清除：{expired}"
    if t == "duming":
        return f"遗物【灾厄轮盘】结算：{eff.get('target')} 失去 {eff.get('damage')} 点生命"
    if t == "self_heal":
        return f"{eff.get('entity')} 触发【自愈】：[回复]{eff.get('actual', eff.get('heal'))}点生命"
    # 2026-10-11：`decay_damage`（【衰败】回始 tick 的战报文案）随道纹删除一并移除
    if t == "bizhong":
        return f"{eff.get('target')} 获得【必中】{eff.get('count')}次"
    if t == "mengbi":
        return f"{eff.get('target')} 获得【蒙蔽】{eff.get('count')}次"
    if t == "boba_register":
        return f"【波及X={eff.get('x')}】结算登记：目标 {eff.get('target')}"
    if t == "beifu":
        return f"{eff.get('caster')} 发动【背负】：替 {eff.get('target')} 承担受到伤害"
    if t == "shanghen":
        return f"{eff.get('target')} 获得【伤痕】"
    if t == "nilin_setup":
        return f"{eff.get('target')} 获得【逆鳞】"
    if t == "self_attack":
        return f"{eff.get('target')} 受自伤类效果影响，攻击自身"
    # 兜底：仍以中文陈述，不直接抛出英文字段名（战报要求全程中文）
    _CN = {"entity": "对象", "target": "目标", "source": "来源", "amount": "数值",
           "value": "数值", "actual_damage": "实际伤害", "raw_damage": "原始伤害",
           "shield_absorbed": "格挡吸收", "hp_before": "生命(前)",
           "hp_after": "生命(后)", "died": "是否命零", "duration": "持续",
           "status": "状态", "note": "说明", "damage_type": "伤害类型"}
    # 已知字段译成中文；未知字段保持 键=值 原样透传（不推算、不编造数值）
    parts = [(f"{_CN[k]}{v}" if k in _CN else f"{k}={v}") for k, v in eff.items()
             if k != "type" and v not in (None, "", 0, False)]
    head = f"结算·{t}" if t else "结算"
    return head + ("：" + "，".join(parts) if parts else "")


def format_player_action(idx: int, actor_name: str, result: dict) -> list[str]:
    """
    渲染我方一次出手。严格取用 execute_action('use_daowen'/'attack') 的返回：
    calculation 给出消耗/代价与公式，execution.effects 给出伤害与生命变化。
    """
    calc = result.get("calculation", {}) or {}
    lines = []
    head = f"出手{idx}（{actor_name}）："
    if calc:
        dw = calc.get("dao_wen", "")
        x = calc.get("x", "")
        cost_type = calc.get("cost_type", "")
        cost = calc.get("cost", "")
        head += f"发动【{dw}X={x}】→{cost_type}{cost}"
    else:
        head += result.get("action", "行动")
    lines.append(head)
    dodge_info = result.get("dodge") or (result.get("execution", {}) or {}).get("dodged")
    if dodge_info and dodge_info.get("fully_dodged"):
        for d in dodge_info.get("dodged_names", []):
            lines.append(f"  → 目标{d.get('name')}声明消耗1点速度闪避，成功（速度→{d.get('speed_after')}），判定与结算完全失效")
    for ef in (result.get("execution", {}) or {}).get("effects", []) or []:
        if ef.get("type") == "damage":
            # 没有格挡时不写"格挡吸收0"，纯属噪音
            absorbed = ef.get("shield_absorbed") or 0
            shield_txt = f"，格挡吸收{absorbed}" if absorbed else ""
            lines.append(
                f"  → 目标{ef.get('target')}：原始伤害{ef.get('raw_damage')}"
                f"{shield_txt}，实际{ef.get('actual_damage')}"
                f"，生命{ef.get('hp_before')}→{ef.get('hp_after')}"
                f"{'（[命零]）' if ef.get('died') else ''}"
            )
        else:
            lines.append(f"  → {_render_effect(ef)}")
    return lines


def format_monster_hits(start_idx: int, details: list) -> list[str]:
    """
    渲染怪物出手。details 来自 monster_phase 的 result['details']，
    每一击一行，闪避显式书写（铁律5），禁止合并。
    """
    lines = []
    idx = start_idx
    for d in details or []:
        if "skipped" in d:
            lines.append(f"出手{idx}（{d.get('monster')}）：{d['skipped']}，无法行动")
            idx += 1
            continue
        if "dragon_breath" in d:
            lines.append(f"出手{idx}（{d.get('monster')}）：受【龙息】必中{d['dragon_breath']}点")
            idx += 1
            continue
        if "daowen_activated" in d:
            x_str = f"X={d.get('x')}" if d.get("x") else ""
            lines.append(f"出手{idx}（{d.get('monster')}）：发动【{d['daowen_activated']}{x_str}】")
            idx += 1
            continue
        if "collapsed" in d:
            lines.append(f"出手{idx}（{d.get('monster')}）：{d.get('note', '暂离结束')}")
            idx += 1
            continue
        atk = d.get("attacker", "?")
        tgt = d.get("target", "?")
        # 一轮攻击(attack_count 次)同属一个攻击出手：共用出手号，逐击缩进书写。
        # 每次攻击仍独立判定闪避（规则正文·基础定义），故每击单独成行，不合并结算。
        total = d.get("hit_total") or 1
        hi = d.get("hit_index") or 1
        multi = total > 1
        if multi and not d.get("new_action", True):
            idx -= 1   # 同一出手的后续击，不占新的出手号
        head = f"出手{idx}（{atk}）" + (f"·第{hi}/{total}击" if multi else "")
        pad = "　　" if multi and hi > 1 else ""
        if d.get("cant_target"):
            lines.append(f"{pad}{head}：选定{tgt}失败——{d.get('note')}")
        elif d.get("retreated"):
            lines.append(
                f"{pad}{head}：攻击{tgt}→伤害{d.get('damage_dealt')}"
                f"——{tgt}即将[命零]，自动【撤退】（保留当前生命{d.get('target_hp_after')}，退出本场）"
            )
        elif d.get("dodge_success"):
            lines.append(
                f"{pad}{head}：攻击{tgt}→{tgt}声明消耗1点速度闪避，成功"
                f"（速度→{d.get('speed_after_dodge')}），判定与结算完全失效"
            )
        else:
            dodge_txt = "声明不闪避" if not d.get("dodge_attempted") else "闪避失败"
            absorbed = d.get("shield_absorbed") or 0
            shield_txt = f"，格挡吸收{absorbed}" if absorbed else ""
            lines.append(
                f"{pad}{head}：攻击{tgt}→{tgt}{dodge_txt}"
                f"→伤害{d.get('damage_dealt')}{shield_txt}"
                f"，失去生命{d.get('hp_lost')}"
                f"{'（[命零]）' if d.get('target_died') else ''}"
            )
        idx += 1
    return lines


def format_round_end(re_result: dict, player: Any, enemies: list) -> list[str]:
    """渲染 [回终]：效果→格挡清空→持续X-1→本回合资源面板。"""
    lines = ["[回终]："]
    effects = (re_result or {}).get("effects", []) or []
    if effects:
        for eff in effects:
            lines.append(f"  → {_render_effect(eff)}")
    else:
        lines.append("  → 无[回终]类效果")
    lines.append("  → 格挡清空；持续X剩余回合-1")
    lines.append("  → 回合末资源面板：")
    lines.append(f"　我方　{resource_line(player)}")
    for e in enemies:
        if e.is_alive:
            lines.append(f"　敌方　{resource_line(e)}")
    return lines


def format_battle_end(be_result: dict) -> list[str]:
    """渲染 [战终] 七步，数值取自 battle_end 返回。"""
    r = be_result or {}
    lines = ["", "[战终]"]
    dead = r.get("removed_via_alt_path", []) or []
    lines.append(f"死亡结算：{'、'.join(x.get('name', str(x)) for x in dead) if dead else '无非击杀移出'}")
    lines.append(f"[碎片]奖励计算：本场奖励{r.get('shard_reward')}，累计{r.get('total_shards')}")
    lines.append("增益与减益清除：清除局内增益（回复/格挡/持续∞）与减益")
    lines.append("代价保留项：代价不随[战终]清除")
    for log in r.get("relic_end_logs", []) or []:
        lines.append(f"  → 遗物[战终]：{log}")
    lines.append("[朋友][员工]留存，[临时朋友]消失")
    lines.append(f"精力恢复：{r.get('energy_restored')}")
    return lines


def validate_battle_report_actions(report_text: str) -> dict:
    """
    程序化校验战报中所有战斗与出手的合规性：
    1. 1出手=1条决策：每一次出手块内部至多包含 1 条**独立**行动声明，严禁把多次独立出手
       打包进同一次出手。注意现行法术架构：一次【瞬发法术】（cast）本身就是一条决策，
       它在一个出手内按步骤依次发动多种道纹、逐步结算（每步各算一次发动道纹）——
       写明「瞬发法术」的出手块按一条决策计，不判违规；
    2. 行动预算：单回合内各角色出手次数严格受限（轮回者按 `action_count`＝基础 2 次＋遗物/【无力】/【蓄锐·增】等修正；报告若写明出手数则按报告），严禁超额出手；
    3. 死斗交替与余量规则：在对手仍有剩余出手预算时，双方严格 1 对 1 对称交替；当一方出手耗尽后，另一方可连续执行剩余出手（符合正文铁律）；
    4. 出手序号必须单调递增。
    若发现任何违规，立即抛出 ValueError 并指出具体场次、回合与出手号。
    """
    import re
    errors = []
    total_actions = 0

    # 拆分战斗场次
    battle_sections = re.split(r"^##\s+", report_text, flags=re.MULTILINE)
    
    for b_idx, section in enumerate(battle_sections):
        if not section.strip():
            continue
        first_line = section.strip().splitlines()[0]
        is_duel = "死斗" in first_line or "第8场" in first_line

        # 提取双方初始面板中的 action_count 出手预算（不从速限换算）
        challenger_budget = 2
        defender_budget = 2
        
        ch_match = re.search(r"挑战[^\n]*?出手(\d+)次", section) or re.search(r"挑战[^\n]*?/\d+/(\d+)", section)
        if ch_match:
            challenger_budget = int(ch_match.group(1))
            
        def_match = re.search(r"守擂[^\n]*?出手(\d+)次", section) or re.search(r"守擂[^\n]*?/\d+/(\d+)", section)
        if def_match:
            defender_budget = int(def_match.group(1))

        # 拆分回合
        round_blocks = re.split(r"第(\d+)回合", section)
        if len(round_blocks) < 2:
            continue

        for r_idx in range(1, len(round_blocks), 2):
            r_num = round_blocks[r_idx]
            r_content = round_blocks[r_idx + 1]

            action_matches = list(re.finditer(r"(^[　]*出手\d+（.+?）.*?)(?=^[　]*出手\d+|^\[怪物行动\]|^\[回终\]|\Z)", r_content, flags=re.MULTILINE | re.DOTALL))
            
            last_actor_side = None
            side_action_counts = {"challenger": 0, "defender": 0}

            for a_match in action_matches:
                total_actions += 1
                a_text = a_match.group(1).strip()
                header_line = a_text.splitlines()[0]

                actor_match = re.search(r"出手\d+（(.+?)）", header_line)
                actor_name = actor_match.group(1) if actor_match else ""

                # 校验1：单次出手内声明的独立主动发动数。
                # 例外：一次【瞬发法术】（cast）是一条决策，其多个步骤在一个出手内
                # 依次发动、逐步结算（R-1：法术每一步 = 一次发动道纹），不是"合并打包"。
                is_instant_spell = bool(re.search(r"瞬发法术|法术流程|多步施法|\bcast\b", a_text))
                daowen_decls = re.findall(r"\[动作声明\].*?发动(?:专属道纹|大招|全力重击|满额|终结大招|终结技)?【(.+?)】|\[动作声明\].*?使用(?:废墟工具)?【(.+?)】|(?<!被动)发动(?:专属道纹|大招|全力重击|满额|终结大招|终结技)?【(.+?)】", a_text)
                real_activations = []
                for d in daowen_decls:
                    act = d[0] or d[1] or d[2]
                    if act and act not in real_activations:
                        real_activations.append(act)

                clean_activations = [x for x in real_activations if "处于" not in x and "获得状态" not in x and "触发" not in x and "被动" not in x]
                if len(clean_activations) > 1 and not is_instant_spell:
                    errors.append(
                        f"[{first_line} 第{r_num}回合 {header_line}] 违规合并发动了多个道纹/能力 "
                        f"({clean_activations})，违反“1出手=1道纹”行动预算铁律！"
                    )

                # 校验2：死斗交替与预算校验（智能支持对手耗尽时余量继续）
                if is_duel and actor_name:
                    current_side = "challenger" if ("莫非" in actor_name or "挑战" in actor_name) else "defender"
                    other_side = "defender" if current_side == "challenger" else "challenger"
                    side_action_counts[current_side] += 1

                    # 检查预算是否超限
                    cur_budget = challenger_budget if current_side == "challenger" else defender_budget
                    if side_action_counts[current_side] > cur_budget:
                        errors.append(
                            f"[{first_line} 第{r_num}回合 {header_line}] {actor_name} 本回合出手次数 "
                            f"({side_action_counts[current_side]}) 超过 action_count 上限 ({cur_budget})！"
                        )

                    # 检查交替合法性：仅当对手阵营仍有剩余行动预算时，才强制交替
                    other_budget = defender_budget if current_side == "challenger" else challenger_budget
                    other_has_remaining = side_action_counts[other_side] < other_budget
                    
                    if last_actor_side == current_side and other_has_remaining:
                        errors.append(
                            f"[{first_line} 第{r_num}回合 {header_line}] 违反死斗交替出手铁律："
                            f"在对手仍有剩余出手 ({side_action_counts[other_side]}/{other_budget}) 时，"
                            f"阵营 {current_side} 连续行动！"
                        )
                    last_actor_side = current_side

    if errors:
        raise ValueError("战报出手合规性校验失败：\n" + "\n".join(errors))

    return {"total_actions_validated": total_actions, "status": "compliant"}


def validate_setup_discovery_text(report_text: str) -> dict:
    """开局段必须同时写出道纹/遗物的候选与选择，缺候选即违规。"""
    import re
    if "[开局]" not in report_text:
        raise ValueError("战报缺少[开局]段")
    dao = re.search(r"初始道纹发现：候选〔(.+?)〕→选择【(.+?)】", report_text)
    relic = re.search(r"遗物发现：候选〔(.+?)〕→选择【(.+?)】", report_text)
    if not dao:
        raise ValueError("开局必须先列出初始道纹发现候选，再写选择")
    if not relic:
        raise ValueError("开局必须先列出遗物发现候选，再写选择")
    dao_opts = [part.strip() for part in dao.group(1).split("、") if part.strip()]
    relic_opts = [part.strip() for part in relic.group(1).split("、") if part.strip()]
    if dao.group(2) not in dao_opts:
        raise ValueError("初始道纹选择不在本次候选中")
    if relic.group(2) not in relic_opts:
        raise ValueError("遗物选择不在本次候选中")
    return {
        "status": "compliant",
        "daowen_options": dao_opts,
        "daowen_pick": dao.group(2),
        "relic_options": relic_opts,
        "relic_pick": relic.group(2),
    }

