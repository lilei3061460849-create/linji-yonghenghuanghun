#!/usr/bin/env python3
"""
实战平衡模拟器
解析全副本索引全部怪物面板，用不同胜利路径策略跑单场战斗，统计胜率。
用于把占位阈值（癌变/还债/雕塑）调到目标胜率。

用法:
    python sim/balance_sim.py            # 跑全部策略 × 全部怪物
    python sim/balance_sim.py kill 200   # 单策略 × 200局/怪
"""
import sys, os, re, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity, GameState, DaoWen, DaoWenInstance, StatusEffect
from engine.combat import CombatEngine
from engine.dice import DiceEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ===== 占位阈值（与 combat.py 保持一致，供调参覆盖） =====
TUNING = {
    "PROLIF_THRESHOLD": 1.0,   # 癌变：累计恢复 ≥ 血限×N
    "DEBT_THRESHOLD": 10,      # 还债：负债 ≤ -N
    "SCULPTURE_DAMAGE": 15,
    "SCULPTURE_SHIELD": 20,
}

REGION_EXCLUSIVE = {
    "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
    # 2026-10-13：删【逼债】→【典当】顶替（发现类，无回始结算，本模拟不建模其发现效果）。
    "罪孽都市": {"点金","典当","抵扣","清算","赎金","假钞","消灾"},
    "龙心谷":   {"加害","龙鳞","逆鳞","裂变","背负","伤痕"},   # 2026-10-11 删【嫁祸】
}

# 各副本怪物池的行范围（用于归属判定）——按面板出现顺序解析即可
def parse_monsters():
    """从全副本索引加载怪物池，而不是直接解析规则正文。"""
    from engine.monsters import parse_monster_pool
    pools = parse_monster_pool(os.path.join(ROOT, "副本索引.md"))
    return [
        {"name": monster["name"], "ac": monster["attack_count"], "ap": monster["attack_power"],
         "hp": monster["blood_limit"], "dw": monster["dao_wen"], "region": region}
        for region, monsters in pools.items() for monster in monsters
    ]


def pool_by_region(monsters, region):
    return [m for m in monsters if m["region"] == region]


def make_monster(md):
    m = Entity(name=md["name"], entity_type="怪物",
               blood_limit=md["hp"], current_hp=md["hp"],
               attack_count=md["ac"], attack_power=md["ap"])
    for n, x in md["dw"].items():
        # 面板不再写死 X，解析结果为 None（发动时自选）。
        # 直接把 None 塞进 x_value 会让后续的 X 算术（如 YUANCHU_COST_RATE * x）
        # 抛 TypeError，故与 engine/monsters.py 同口径：None → x_free，值暂记 0。
        m.dao_wen[n] = DaoWenInstance(
            dao_wen=DaoWen(name=n, formula="", cost_type="", cost_formula="", effect_formula=""),
            x_value=(x if x is not None else 0), x_free=(x is None))
    # 副本专属道纹运行态（裁定⑨）
    m.fake_shards = 0; m.shards = 0
    m._beifu_left = 0; m._beifu_target = None
    m._nilin = 0
    return m


def make_player(extra_dw=None):
    # 标准构筑：10血/7速/8法 → 60HP/14法/8速(3出手)，杀伐+庇护+再生
    p = Entity(name="轮回者", entity_type="轮回者",
               blood_limit=60, current_hp=60,
               mana_limit=14, current_mana=14,
               speed_limit=8, current_speed=8,
               attack_count=1, attack_power=1)
    for n in ["杀伐", "庇护", "再生"] + (extra_dw or []):
        p.dao_wen[n] = DaoWenInstance(
            dao_wen=DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
            x_value=0)
    return p


# ===== 道纹效果（模拟版，忠实公式） =====
def cast_shaifa(player, monster, x, monsters=None):
    if player.current_mana < x: return 0
    player.current_mana -= x
    if player.has_status("蒙蔽"):  # 蒙蔽：本次伤害无效，层数-1
        for s in player.status_effects:
            if s.name == "蒙蔽" and s.value > 0:
                s.value -= 1
                if s.value <= 0: player.status_effects.remove(s)
                break
        return 0
    dmg = 2 * eff_x(player, x)  # 退化：本次数值-X
    return hit_monster(player, monster, dmg, monsters or [monster])

def cast_bihu(player, x):
    x = eff_x(player, x)
    if player.current_mana < x: return 0
    player.current_mana -= x
    player.shield += 4 * x
    return 4 * x

def cast_zaisheng(player, target, x):
    x = eff_x(player, x)
    if player.current_mana < x: return 0
    if USE_EXCLUSIVE and player.has_status("坏死"): return 0  # 坏死禁疗生效（裁定⑨补漏，此前只挂状态未拦截）
    player.current_mana -= x
    before = target.current_hp
    amount = 6 * x
    target.current_hp = min(target.blood_limit, target.current_hp + amount)
    actual = target.current_hp - before
    target.total_healed += actual + (amount - actual) * 2
    return actual

def cast_ziyang(player, monster, x):  # 滋养X：治疗血限10X%
    x = eff_x(player, x)
    if player.current_mana < 5*x: return 0
    player.current_mana -= 5*x
    heal = math.ceil(monster.blood_limit * 10 * x / 100)
    monster.heal(heal)  # heal() 自动按过量双倍计入total_healed
    return heal

def cast_ruohua(player, monster, x):  # 弱化X：攻击力-X（消耗3X）
    x = eff_x(player, x)
    if player.current_mana < 3*x: return 0
    player.current_mana -= 3*x
    monster.attack_power = max(0, monster.attack_power - x)
    return x

def cast_shujin(player, monster, x):  # 赎金X：夺10X碎片（消耗10X）；失去碎片优先假碎片
    x = eff_x(player, x)
    if player.current_mana < 10*x: return 0
    player.current_mana -= 10*x
    amt = 10 * x
    fake = min(getattr(monster, "fake_shards", 0), amt)
    monster.fake_shards = getattr(monster, "fake_shards", 0) - fake
    monster.shards -= (amt - fake)
    return amt


def monster_round_start(m, activated):
    """怪物回始被动：自愈/庇护（须已激活才生效；原始道纹只在首次发动时支付异变）。"""
    if "自愈" in activated:
        x = m.dao_wen["自愈"].x_value
        heal = math.ceil(m.blood_limit * 10 * x / 100)
        m.heal(heal)
    if "庇护" in activated:
        x = m.dao_wen["庇护"].x_value
        m.shield += 4 * x


def monster_activate(m, activated, rng):
    """
    怪物道纹出手：激活一个尚未激活的道纹（白板第1回合后开始激活），返回激活名或None
    成长型：疯狂X攻击出手+X；全力X攻击力+X；狂暴+1攻击出手；必中不可闪避；自愈/庇护回始生效
    控场型（对轮回者）：蒙蔽X下X次伤害无效；坏死禁疗；减速速度减半；僵化攻击力固定1
    【异变计费接线，裁定②】原始怪物道纹以【异变】为代价：激活支付异变5×面板X；
    达阈值触发【崩解】直接命零，返回"崩解:道纹名"，本次激活效果中断。
    """
    priority = ["疯狂", "全力", "狂暴", "必中", "蒙蔽", "坏死", "减速", "僵化", "自愈", "庇护", "飞行"]
    for g in priority:
        if g in m.dao_wen and g not in activated:
            if g in CombatEngine.ORIGINAL_MONSTER_DAOWEN:
                pay = m.add_mutation(CombatEngine.YUANCHU_COST_RATE * m.dao_wen[g].x_value)
                if pay["collapsed"]:
                    SIM_STATS["collapses"] += 1
                    return "崩解:" + g
            activated.add(g)
            if g == "全力":
                m.attack_power += m.dao_wen[g].x_value
            return g
    if USE_EXCLUSIVE:  # 副本专属层（裁定⑨）：通用层之后，代价未满足的跳过
        for g in EXCLUSIVE_PRIORITY:
            if g in m.dao_wen and g not in activated and monster_can_pay_exclusive(m, g):
                activated.add(g)
                return g
    return None


# 模拟统计（裁定②接线）：供跑批报告进化/崩解发生频次
SIM_STATS = {"evolutions": 0, "collapses": 0}

# 困境进化默认策略的借用优先级（在原始道纹范围内按模拟器激活优先级排序）
EVO_PRIORITY = [d for d in ["疯狂", "全力", "狂暴", "必中", "蒙蔽", "坏死", "减速", "僵化", "自愈", "庇护", "飞行"]
                if d in CombatEngine.ORIGINAL_MONSTER_DAOWEN]


def sim_maybe_evolve(m, combat):
    """
    怪物困境默认进化策略（裁定②接线）：借优先级最高的未持有原始道纹，
    怪物免于迷失；异变不限制 X，策略使用通用试探封顶值。
    困境判定与参数校验全部走引擎 execute_evolution（事实源），此处只负责选择。
    返回执行结果dict或None。
    """
    if not m.is_alive:
        return None
    target = next((d for d in EVO_PRIORITY if d not in m.dao_wen), None)
    if target is None:
        return None
    max_x = (combat._DAOWEN_X_PROBE_CAP if not m.can_trigger_mutation_lost() else
             (Entity.MUTATION_COLLAPSE_THRESHOLD - 1 - m.mutation_count) // CombatEngine.YUANCHU_COST_RATE)
    if max_x < 1:
        return None
    r = combat.execute_evolution(m, target, max_x)
    if r.get("success"):
        SIM_STATS["evolutions"] += 1
        return r
    return None


def apply_control_to_player(name, m, player):
    """怪物激活控场道纹后，对轮回者施加效果"""
    x = m.dao_wen[name].x_value if name in m.dao_wen else 1
    if name == "蒙蔽":
        player.add_status(StatusEffect("蒙蔽", remaining_rounds=-1, value=x))
    elif name == "坏死":
        player.add_status(StatusEffect("坏死", remaining_rounds=-1, value=0))
    elif name == "减速":
        player.current_speed = max(0, player.current_speed // 2)
    elif name == "僵化":
        player.attack_power = 1  # 轮回者以杀伐为主，影响小


# =========================================================================
# 副本专属道纹实装（裁定⑨ 2026-08-10：全部24种专属道纹建模后重测平衡）
# 语义事实源：规则正文道纹定义 + engine/combat.py 已实现口径；遗物【灾厄轮盘】不属于怪物道纹池。
# USE_EXCLUSIVE=False 回到未建模基线（A/B对照用）。
# 已知本模拟器结构性空转（照激活、记计数、无数值影响）：超频（怪速度未入战斗数学）、
# 定型（模拟玩家战斗中不涨攻击/次数）、抵扣（模拟玩家无遗物）。
# =========================================================================
USE_EXCLUSIVE = True

# 激活顺序（接在通用优先级之后；每回合至多激活1个，激活=出手）
EXCLUSIVE_PRIORITY = ["变形", "逆鳞", "假钞", "加害", "点金", "赎金",
                      "清算", "龙鳞", "爆裂", "裂变", "伤痕", "退化", "畸变", "活血",
                      "背负", "超频", "定型", "抵扣"]  # 消灾只改写骰点；该简化模拟无生产骰点管线，故不模拟/不激活
REDIRECT_ATTRS = ("_beifu_left", "_beifu_target", "_nilin")   # 2026-10-11 删【嫁祸】


def eff_x(player, x):
    """【退化X】：玩家发动道纹时该次数值-X，最低0（持续∞）"""
    if not USE_EXCLUSIVE or not player.has_status("退化"):
        return x
    return max(0, x - player.get_status_value("退化"))


def _alive_monsters(monsters):
    return [m for m in monsters if m.is_alive and not (
        getattr(m, "is_sculptured", False)
        or getattr(m, "is_proliferated", False) or getattr(m, "is_debt_bound", False))]


def hit_monster(player, m, dmg, monsters, _redirected=False):
    """
    玩家→怪物伤害统一漏斗：背负转嫁 → 裂变分次 → 龙鳞减伤 → 爆裂反射 → 逆鳞/活血记账。
    返回实际落到m(或承担者)身上的总伤害。
    """
    if dmg <= 0 or not m.is_alive:
        return 0
    if USE_EXCLUSIVE and not _redirected:
        # 背负X：其他怪物声明替m承担其下X次伤害
        for b in _alive_monsters(monsters):
            if b is not m and getattr(b, "_beifu_left", 0) > 0 and getattr(b, "_beifu_target", None) == m.runtime_id:
                b._beifu_left -= 1
                return hit_monster(player, b, dmg, monsters, _redirected=True)
    # 裂变X：分X次结算，每次=原伤害÷X向下取整
    parts = [dmg]
    if USE_EXCLUSIVE and m.has_status("裂变"):
        xv = max(1, m.get_status_value("裂变"))
        parts = [dmg // xv] * xv
    total = 0
    for d in parts:
        if USE_EXCLUSIVE and m.has_status("龙鳞"):  # 每次受到伤害-X，最低0
            d = max(0, d - m.get_status_value("龙鳞"))
        total += d
    # 爆裂X：受到伤害前，攻击者失去等量生命（按本次结算总量反射一次）
    if USE_EXCLUSIVE and m.has_status("爆裂") and total > 0:
        _raw_hit_player(player, total)
    m.current_hp = max(0, m.current_hp - total)
    m.hp_lost_this_round += total
    if USE_EXCLUSIVE and m.has_status("逆鳞"):
        m._nilin = getattr(m, "_nilin", 0) + total  # 每失去1点生命获得1层逆鳞
    if m.current_hp <= 0:
        m.is_alive = False
    return total


def _raw_hit_player(player, dmg):
    """不经护盾/闪避的直接生命损失（爆裂反射等；本模拟不建模遗物【灾厄轮盘】）"""
    player.current_hp = max(0, player.current_hp - dmg)
    player.hp_lost_this_round += dmg
    if player.current_hp <= 0:
        player.is_alive = False
    return dmg


def _hurt_player_track(player, lost):
    """玩家失去生命后的通用钩子：伤痕X（每次失去生命后[血限]-X）"""
    if USE_EXCLUSIVE and lost > 0 and player.has_status("伤痕"):
        x = player.get_status_value("伤痕")
        player.blood_limit = max(0, player.blood_limit - x)
        player.current_hp = min(player.current_hp, player.blood_limit)
        if player.blood_limit <= 0:
            player.is_alive = False


def exclusive_round_start(player, monsters, activated_map, rng):
    """回始：清算；遗物【灾厄轮盘】不在此道纹专属模拟器的建模范围。"""
    if not USE_EXCLUSIVE:
        return
    for m in _alive_monsters(monsters):
        act = activated_map.get(id(m), set())
        # 2026-10-13：【逼债】删除，原回始负债分支一并移除。
        if m.has_status("清算"):
            drain = max(0, getattr(m, "shards", 0))
            player.shield = max(0, player.shield - drain)


def exclusive_round_end(player, monsters):
    """回终：畸变（失血限=攻×次）/活血（回失血半数）/持续型状态递减/逆鳞过期清层"""
    if not USE_EXCLUSIVE:
        return
    if player.has_status("畸变"):
        x = player.get_status_value("畸变")
        loss = player.attack_power * player.attack_count  # 畸变X：失去(攻击力×攻击次数)血限
        player.blood_limit -= loss
        player.current_hp = min(player.current_hp, player.blood_limit)
        if player.current_hp <= 0:
            player.is_alive = False
    for ent in [player] + list(monsters):
        for st in list(ent.status_effects):
            if st.remaining_rounds > 0:
                st.remaining_rounds -= 1
                if st.remaining_rounds <= 0:
                    if st.name == "逆鳞":
                        ent._nilin = 0
                    if st.name == "变形":  # 变形到期还原互换
                        ent.attack_power, ent.attack_count = ent.attack_count, ent.attack_power
                    ent.status_effects.remove(st)
        if isinstance(ent, Entity) and ent.entity_type == "怪物" and ent.has_status("活血") and ent.is_alive:
            x = ent.get_status_value("活血")
            heal = ent.hp_lost_this_round // 2
            if heal > 0:
                ent.heal(heal)


def apply_exclusive(act, m, player, monsters, rng):
    """怪物激活副本专属道纹的效果施加（对照规则正文定义；空转型照激活记计数）"""
    x = m.dao_wen[act].x_value
    if act == "变形":      # 自身攻击力与攻击次数互换，持续X
        m.attack_power, m.attack_count = m.attack_count, m.attack_power
        m.add_status(StatusEffect("变形", remaining_rounds=x, value=x))
    elif act == "逆鳞":    # 代价流血X（自流失血也叠层）；持续X内每次失血+1层，下次伤害+全部层
        m.current_hp = max(0, m.current_hp - x); m.hp_lost_this_round += x
        m._nilin = getattr(m, "_nilin", 0) + x
        if m.current_hp <= 0: m.is_alive = False; return
        m.add_status(StatusEffect("逆鳞", remaining_rounds=x, value=x))
    elif act == "假钞":    # 获得10X假碎片
        m.fake_shards = getattr(m, "fake_shards", 0) + 10 * x
    elif act == "加害":    # 引擎口径（攻击者侧）：造成的伤害+X，持续∞
        m.add_status(StatusEffect("加害", remaining_rounds=-1, value=x))
    elif act == "点金":    # DM 2026-09-10：消耗8X法力直接换X真碎片，与伤害无关
        if m.current_mana >= 8 * x:       # 前身【洗劫】按实伤夺碎片，已废止
            m.current_mana -= 8 * x
            m.shards += x
    # 2026-10-13：【逼债】删除（原分支只是 pass 记账，无行为）。
    elif act == "赎金":    # 夺10X碎片；目标没有碎片则失X点当前速度（一次性）
        if player.shards > 0:
            steal = min(player.shards, 10 * x); player.shards -= steal
        else:
            player.current_speed = max(0, player.current_speed - x)
    elif act == "清算":    # 回始目标失[你碎片]点格挡，持续X
        m.add_status(StatusEffect("清算", remaining_rounds=x, value=x))
    elif act == "龙鳞":
        m.add_status(StatusEffect("龙鳞", remaining_rounds=-1, value=x))
    elif act == "爆裂":
        m.add_status(StatusEffect("爆裂", remaining_rounds=x, value=x))
    elif act == "裂变":
        m.add_status(StatusEffect("裂变", remaining_rounds=-1, value=x))
    elif act == "伤痕":
        player.add_status(StatusEffect("伤痕", remaining_rounds=-1, value=x))
    elif act == "退化":
        player.add_status(StatusEffect("退化", remaining_rounds=-1, value=x))
    elif act == "畸变":
        player.add_status(StatusEffect("畸变", remaining_rounds=x, value=x))
    elif act == "活血":
        m.add_status(StatusEffect("活血", remaining_rounds=x, value=x))
    elif act == "背负":
        others = [b for b in _alive_monsters(monsters) if b is not m]
        if not others:
            return False  # 无同伴可守护：本道纹当前无合法目标，视为不出手此道纹
        m._beifu_target = min(others, key=lambda b: b.current_hp).runtime_id  # 守护最濒危同伴
        m._beifu_left = x
    elif act in ("超频", "定型", "抵扣"):
        m.add_status(StatusEffect(act, remaining_rounds=-1, value=x))  # 结构性空转（见模块注释）
    return True


def monster_can_pay_exclusive(m, g):
    """专属道纹激活的代价准入：背负需有同伴。"""
    x = m.dao_wen[g].x_value
    return True



# ===== 疯狂（2026-08-17 全局裁定；裁定⑫其余候选已归档删除 2026-08-11） =====
# 现行（engine侧）：疯狂X → 所有角色出手次数+X，持续∞；异变5X仅在首次发动时支付。
# 本模拟器镜像只覆盖怪物自身攻击出手+X（发动方视角与全局等价）；
# 玩家侧与其他怪物的+X尚未建模，疯狂相关模拟数据须按 B6 重测后才可引用。
# 已归档候选（AI_EXPERIENCE 追记4）：charges / flat / burst / half — 已从本文件彻底删除，仅保留 current。
# 归档原因：扫描 300局/副本 现行面板，疯狂任何形态对 60HP/8闪速度玩家均为断崖致命（甲乙丁 0~2%，丙 99% 因自崩解），
# 无单点方案可击中 30% 目标，需组合方案或覆盖率杠杆另议。

def get_monster_attack_actions(m, activated):
    """怪物攻击出手数（现行口径）：1 + 疯狂X(已激活) + 狂暴1

    引擎2026-08-17全局裁定后，怪物侧+X由自身疯狂状态驱动；
    此处按激活集合等价模拟（发动方视角），玩家侧+X未建模（见B6重测）。
    """
    n = 1
    if "疯狂" in activated:
        n += m.dao_wen["疯狂"].x_value
    if "狂暴" in activated:
        n += 1
    return n


def monster_attack_round(m, player, combat, rng, must_hit):
    """怪物1轮攻击出手（attack_count次），玩家逐次决定闪避。返回对玩家造成的生命损失。"""
    # 探照灯等施加的怪物侧蒙蔽：本次攻击出手无效，层数-1
    if m.has_status("蒙蔽"):
        for s in list(m.status_effects):
            if s.name == "蒙蔽" and s.value > 0:
                s.value -= 1
                if s.value <= 0: m.status_effects.remove(s)
                break
        return 0
    hp_before = player.current_hp
    nilin_applied = False
    for _ in range(max(0, m.attack_count - getattr(m, "_nade_minus", 0))):  # 高爆手雷：攻击次数-1
        if not player.is_alive or not m.is_alive:
            break
        dmg = m.attack_power
        if USE_EXCLUSIVE and m.has_status("加害"):
            dmg += m.get_status_value("加害")  # 加害X：造成伤害+X
        if USE_EXCLUSIVE and getattr(m, "_nilin", 0) and not nilin_applied:
            dmg += m._nilin; m._nilin = 0; nilin_applied = True  # 逆鳞：下次伤害+全部层后清空
        will_break = dmg > player.shield
        if will_break and player.current_speed > 0 and not must_hit:
            player.current_speed -= 1  # 闪避成功
            continue
        if dmg <= 0:
            continue
        absorbed = min(player.shield, dmg)
        player.shield -= absorbed
        got = dmg - absorbed
        player.current_hp = max(0, player.current_hp - got)
        player.hp_lost_this_round += got
        if USE_EXCLUSIVE and got > 0 and m.has_status("点金"):  # 点金X：夺等量碎片
            player.shards = max(0, player.shards - got)
        if player.current_hp <= 0:
            player.is_alive = False
    lost = hp_before - player.current_hp
    _hurt_player_track(player, lost)  # 伤痕X：失血后血限-X
    return lost


def run_battle(md, policy, rng, player_dw=None):
    """跑一场战斗，返回 {'win':bool, 'path':str, 'rounds':int}"""
    state = GameState()
    state.current_region = md["region"]
    player = make_player(player_dw)
    state.player = player
    monster = make_monster(md)
    state.enemies.append(monster)
    combat = CombatEngine(state, DiceEngine())
    combat.PROLIFERATION_THRESHOLD = TUNING["PROLIF_THRESHOLD"]
    combat.DEBT_THRESHOLD = TUNING["DEBT_THRESHOLD"]
    combat.SCULPTURE_DAMAGE = TUNING["SCULPTURE_DAMAGE"]
    combat.SCULPTURE_SHIELD = TUNING["SCULPTURE_SHIELD"]
    combat.init_monster_shards(monster)
    combat.reset_monster_activation()  # 进化记录按战斗重置（裁定②接线）

    max_rounds = 25
    player.shards = 60  # 单场假设：携带60碎片入场（裁定⑨，供罪孽经济道纹咬合）
    monsters_list = [monster]
    activated = set()  # 怪物已激活的道纹（白板第1回合为空）
    player.current_mana = 0  # 战始清零；回始再获得等同法限的法力
    for rnd in range(1, max_rounds+1):
        if not player.is_alive:
            return {"win": False, "path": "death", "rounds": rnd}
        if not monster.is_alive:
            break
        # 回始：获得等同当前法限的法力
        player.current_mana += player.mana_limit
        player.shield = 0
        player.hp_lost_this_round = 0
        monster.hp_lost_this_round = 0
        monster_round_start(monster, activated)
        # 怪物道纹出手（第1回合白板不激活；首次发动才支付异变）
        if rnd > 1 and monster.is_alive:
            act = monster_activate(monster, activated, rng)
            if act and not act.startswith("崩解:"):  # 崩解：激活效果中断
                apply_control_to_player(act, monster, player)
                if USE_EXCLUSIVE and act in EXCLUSIVE_PRIORITY:
                    apply_exclusive(act, monster, player, monsters_list, rng)
            sim_maybe_evolve(monster, combat)  # 困境进化默认策略（裁定②接线）
        if not monster.is_alive:  # 崩解命零（异变计费）
            break
        exclusive_round_start(player, monsters_list, {id(monster): activated}, rng)  # 专属道纹回始
        if not player.is_alive:
            return {"win": False, "path": "death", "rounds": rnd}

        # 玩家出手（3次），按策略
        actions = 3
        policy(state, player, monster, combat, actions, rng)

        if not monster.is_alive:
            break
        # 怪物攻击出手
        must_hit = "必中" in activated
        n_act = get_monster_attack_actions(monster, activated)
        for i in range(n_act):
            if not player.is_alive:
                break
            monster_attack_round(monster, player, combat, rng, must_hit)
        if not player.is_alive:
            return {"win": False, "path": "death", "rounds": rnd}

        # 回终：格挡/法力清空，专属道纹回终结算，结算胜利路径
        player.shield = 0
        player.current_mana = 0
        monster.shield = 0
        exclusive_round_end(player, monsters_list)
        if not player.is_alive:
            return {"win": False, "path": "death", "rounds": rnd}
        if not monster.is_alive:
            return {"win": True, "path": "kill", "rounds": rnd}
        paths = combat.settle_victory_paths()
        if paths:
            return {"win": True, "path": paths[0]["type"], "rounds": rnd}
    # 超时判定
    if not monster.is_alive:
        return {"win": True, "path": "kill", "rounds": max_rounds}
    return {"win": False, "path": "timeout", "rounds": max_rounds}


# ===== 策略 =====
def strategy_kill(state, p, m, combat, actions, rng):
    # 每回合：够杀就全杀伐；否则庇护挡住下回合伤害，剩余杀伐
    monster_dmg = m.attack_count * m.attack_power
    mana = p.current_mana
    # 致死判定：剩余出手全杀伐能否击杀
    need = math.ceil(m.current_hp / 2)  # 杀伐2X
    if need <= mana and need <= actions * mana:  # 法力够大致击杀
        # 集中杀伐
        while actions > 0 and m.is_alive and p.current_mana > 0:
            x = min(p.current_mana, math.ceil(m.current_hp / 2))
            if x < 1: break
            cast_shaifa(p, m, x)
            actions -= 1
        return
    # 防御：庇护挡住monster_dmg
    need_shield_x = max(1, math.ceil(monster_dmg / 4)) if monster_dmg > 0 else 0
    if monster_dmg > 0 and p.current_hp <= monster_dmg + 5:
        cast_bihu(p, need_shield_x)
        actions -= 1
    # 剩余杀伐
    while actions > 0 and m.is_alive and p.current_mana > 0:
        x = min(p.current_mana, 7)
        cast_shaifa(p, m, x)
        actions -= 1


def strategy_sculpt(state, p, m, combat, actions, rng):
    # 弱化把攻击力打到0→雕塑；期间庇护/闪避生存
    monster_dmg = m.attack_count * m.attack_power
    # 先保证生存
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x and actions > 0:
            cast_bihu(p, need_x)
            actions -= 1
    # 弱化
    while actions > 0 and m.attack_power > 0 and p.current_mana >= 3:
        cast_ruohua(p, m, min(m.attack_power, p.current_mana // 3))
        actions -= 1
    while actions > 0 and p.current_mana > 0 and m.is_alive:
        cast_shaifa(p, m, min(p.current_mana, 7))
        actions -= 1


def strategy_proliferate(state, p, m, combat, actions, rng):
    # 滋养/再生把怪物奶过阈值→癌变；期间庇护生存
    monster_dmg = m.attack_count * m.attack_power
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x and actions > 0:
            cast_bihu(p, need_x)
            actions -= 1
    # 滋养（大奶）
    while actions > 0 and p.current_mana >= 5:
        cast_ziyang(p, m, 1)
        actions -= 1
    while actions > 0 and p.current_mana >= 1:
        cast_zaisheng(p, m, min(p.current_mana, 5))
        actions -= 1


def strategy_debt(state, p, m, combat, actions, rng):
    # 赎金把碎片打到负债→还债（仅罪孽都市怪物有碎片）
    monster_dmg = m.attack_count * m.attack_power
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x and actions > 0:
            cast_bihu(p, need_x)
            actions -= 1
    while actions > 0 and p.current_mana >= 10:
        cast_shujin(p, m, 1)
        actions -= 1
    while actions > 0 and p.current_mana > 0 and m.is_alive:
        cast_shaifa(p, m, min(p.current_mana, 7))
        actions -= 1


STRATEGIES = {
    "kill": (strategy_kill, None),
    "sculpt": (strategy_sculpt, ["弱化"]),
    "proliferate": (strategy_proliferate, ["滋养"]),
    "debt": (strategy_debt, ["赎金"]),
}


def main():
    monsters = parse_monsters()
    print(f"解析到 {len(monsters)} 个怪物\n")
    rng = random.Random(42)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    strats = [only] if only else list(STRATEGIES.keys())
    for sname in strats:
        policy, extra = STRATEGIES[sname]
        wins = 0; total = 0; path_counts = {}
        per_monster = []
        for md in monsters:
            # 还债只对有碎片的罪孽都市怪有意义；其他策略全跑
            ww = 0
            for _ in range(reps):
                r = run_battle(md, policy, rng, player_dw=extra)
                if r["win"]:
                    ww += 1
                    path_counts[r["path"]] = path_counts.get(r["path"], 0) + 1
            total += reps
            wins += ww
            per_monster.append((md["name"], md["region"], ww, reps))
        rate = wins / total * 100 if total else 0
        print(f"=== 策略 [{sname}] ===  胜率 {rate:.1f}% ({wins}/{total})")
        print(f"   路径分布: {path_counts}")
        # 各怪胜率简表（只列<20%和>80%的极端）
        hard = [x for x in per_monster if x[2]/x[3] < 0.2]
        easy = [x for x in per_monster if x[2]/x[3] > 0.8]
        if hard: print(f"   极难(<20%): {[(x[0],x[2]/x[3]) for x in hard]}")
        if easy: print(f"   极易(>80%): {[(x[0],x[2]/x[3]) for x in easy]}")
        print()


if __name__ == "__main__":
    main()
