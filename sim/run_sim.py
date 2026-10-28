#!/usr/bin/env python3
"""
7场全通关率模拟器（一阶副本）
- 出怪: 本模拟器独立复现「一阶上界 = max(1, 战斗场数-battle_offset)」并按确定值放首怪+R4/R7/R10增援，
  与引擎的配方式出怪（2026-09-28 用户令：N=随机(1,上界)、S 只首发、T_i/R_i 随机波次）口径不一致；
  本文件用于快速扫参，不对战始出怪的具体回合节奏做保真。完整出怪事实源见 engine/monsters.py::roll_spawn_plan。
- HP/碎片跨场带；每场前局外(3精力)休整/学习；战终碎片奖励、造物消失、精力回3
- 测真正"通关率"=7场全清的比例 + 最远场分布 + 各胜利路径使用次数

用法: python sim/run_sim.py [runs]
"""
import sys, os, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
_spec = importlib.util.spec_from_file_location("bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_sim.py"))
bs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bs)

from engine.models import Entity, GameState, DaoWen, DaoWenInstance
from engine.combat import CombatEngine
from engine.dice import DiceEngine

# 覆盖阈值（与combat.py一致）
bs.TUNING.update({"PROLIF_THRESHOLD":1.0,"DEBT_THRESHOLD":10})

# ===== 可调设计参数（扫参用） =====
PARAMS = {
    "battle_offset": 3,      # 出怪 = max(1, n - offset)；2→1/1/1/2/3/4/5
    "rest_tier3": 48, "rest_tier3_cost": 25,
    "rest_tier2": 24, "rest_tier2_cost": 10,
    "rest_tier1": 8,
    "grow": "mix",           # 修行方向: speed/mana/mix
    "cap": 99,             # 每场怪物数上限
    "use_tools": True,       # 扭曲都市：前2场局外各花1精力探索→附赠工具库（裁定⑬）
}


def make_run_player():
    # 真实开局：仅杀伐，60HP/14法/8速
    p = Entity(name="轮回者", entity_type="轮回者", blood_limit=60, current_hp=60,
               mana_limit=14, current_mana=14, speed_limit=8, current_speed=8,
               attack_count=1, attack_power=1)
    p.dao_wen["杀伐"] = bs.DaoWenInstance(
        dao_wen=DaoWen(name="杀伐",formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
    p.tools = []  # 工具库消耗品 [{name, uses}]（裁定⑬：扭曲都市探索附赠）
    return p

from engine.api import TWISTED_TOOL_LIBRARY

def tools_grant(player, rng):
    """扭曲都市：完成事件附赠【发现】，从未持有工具库随机获得1件（发现3选1简化为随机）"""
    unowned = [n for n in TWISTED_TOOL_LIBRARY if all(t["name"] != n for t in player.tools)]
    if unowned:
        name = rng.choice(unowned)
        player.tools.append({"name": name, "uses": TWISTED_TOOL_LIBRARY[name][0]})
        return name
    return None

def _focus(monsters):
    alive = alive_monsters(monsters)
    return min(alive, key=lambda m: m.current_hp) if alive else None

def _use(player, name):
    for t in player.tools:
        if t["name"] == name and t["uses"] > 0:
            t["uses"] -= 1
            return True
    return False

def _has(player, name):
    return any(t["name"] == name and t["uses"] > 0 for t in player.tools)

def tools_phase(player, monsters, activated, rnd):
    """回始道具阶段（裁定⑬；使用不消耗出手）。每个种类每回合至多1次。"""
    alive = alive_monsters(monsters)
    if not alive:
        return
    focus = _focus(monsters)
    # 干扰仪：≥2敌时封本场激活（本回合）
    if len(alive) >= 2 and _has(player, "干扰仪"):
        _use(player, "干扰仪")
        for m in alive: m._jammed = True
    # 储能电池：回始+12法力
    if rnd <= 3 and _has(player, "储能电池"):
        _use(player, "储能电池"); player.current_mana += 12
    # 探照灯：集火目标蒙蔽2（怪物侧=下2次攻击出手无效）
    if focus is not None and _has(player, "强光探照灯"):
        _use(player, "强光探照灯")
        focus.add_status(bs.StatusEffect("蒙蔽", remaining_rounds=-1, value=2))
    # 高压水枪：敌方任何"持续X"效果≥1时清全场敌方持续效果
    if any(st.remaining_rounds > 0 for m in alive for st in m.status_effects) and _has(player, "高压水枪"):
        _use(player, "高压水枪")
        for m in alive:
            m.status_effects = [s for s in m.status_effects if s.remaining_rounds <= 0]
    # 备用血泵：生命≤50%时回复20；≤30%额外30格挡
    if player.current_hp <= player.blood_limit * 0.5 and _has(player, "备用血泵"):
        _use(player, "备用血泵")
        player.current_hp = min(player.blood_limit, player.current_hp + 20)
        if player.current_hp <= player.blood_limit * 0.3: player.shield += 30
    # 急救箱：生命≤35%时回复25并清一种负面持续
    if player.current_hp <= player.blood_limit * 0.35 and _has(player, "急救箱"):
        _use(player, "急救箱")
        player.current_hp = min(player.blood_limit, player.current_hp + 25)
        neg = [s for s in player.status_effects if s.remaining_rounds > 0 or s.name in ("坏死","退化","伤痕","畸变","蒙蔽")]
        if neg: player.status_effects.remove(neg[0])
    # 高爆手雷：集火目标15伤害+本回合攻击次数-1
    if focus is not None and _has(player, "高爆手雷"):
        _use(player, "高爆手雷")
        bs.hit_monster(player, focus, 15, monsters)
        focus._nade_minus = getattr(focus, "_nade_minus", 0) + 1
    # 反怪物电击枪：集火25伤害（飞行+15）
    focus = _focus(monsters)
    if focus is not None and _has(player, "反怪物电击枪"):
        _use(player, "反怪物电击枪")
        flying = focus.is_flying or "飞行" in activated.get(id(focus), set())
        bs.hit_monster(player, focus, 25 + (15 if flying else 0), monsters)



def has_dw(p, n): return n in p.dao_wen
def learn(p, n):
    p.dao_wen[n] = bs.DaoWenInstance(
        dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))

def cast_boba(player, monsters, x):
    """波及X：消耗3X法力，标记前X个存活怪（2026-08-21：冲击改名波及，无直接伤害）"""
    if player.current_mana < 3 * x: return 0
    player.current_mana -= 3 * x
    marked = 0
    for m in monsters:
        if marked >= x: break
        if m.is_alive and not (m.is_sculptured or m.is_proliferated or m.is_debt_bound):
            from engine.models import StatusEffect
            m.add_status(StatusEffect(name="波及", remaining_rounds=-1, value=1, source=player.name))
            marked += 1
    return marked


# 修行档：(属性点, 碎片消耗)，按性价比降序
XIUXING_TIERS = [(6,150),(5,100),(4,65),(3,35),(2,15),(1,0)]

def pre_battle_prep(player, shards, energy=3, battle_n=1, region=None, rng=None):
    """局外：激进花碎片修行（碎片不转化为面板就是废物）；仅低血才休整。
    扭曲都市且use_tools：前2场各花1精力探索（完成事件附赠工具库1件）。"""
    if PARAMS.get("use_tools") and region == "扭曲都市" and battle_n <= 2 and energy > 0 and rng is not None:
        got = tools_grant(player, rng)
        if got: energy -= 1
    if battle_n == 1:
        for dw in ["庇护", "再生", "波及"]:
            if not has_dw(player, dw) and energy > 0:
                learn(player, dw); energy -= 1
    while energy > 0:
        if player.current_hp < player.blood_limit * 0.35 and shards >= 25:
            player.current_hp = min(player.blood_limit, player.current_hp + 48); shards -= 25; energy -= 1
        elif player.current_hp < player.blood_limit * 0.2:
            player.current_hp = min(player.blood_limit, player.current_hp + 8); energy -= 1
        else:
            # 买能负担的最高修行档，多点数/精力；出手<5偏速限，否则法限
            for pts, cost in XIUXING_TIERS:
                if shards >= cost:
                    shards -= cost
                    for _ in range(pts):
                        if player.speed_limit < 5:
                            player.speed_limit += 1
                        else:
                            player.mana_limit += 2
                    player.current_speed = player.speed_limit
                    break
            energy -= 1
    player.current_mana = player.mana_limit
    return shards


def alive_monsters(monsters):
    return [m for m in monsters if m.is_alive and not (m.is_sculptured
            or m.is_proliferated or m.is_debt_bound)]


def player_turn_multi(player, monsters, combat, rng):
    """多怪玩家回合：先标记波及再焦点最低血怪；低血再生、大伤害庇护"""
    mana = player.current_mana
    actions = max(1, player.action_count)
    alive = alive_monsters(monsters)
    if not alive: return
    incoming = sum(m.attack_count * m.attack_power for m in alive)
    if player.current_hp <= 0.35 * player.blood_limit and mana >= 2 and has_dw(player,"再生"):
        bs.cast_zaisheng(player, player, min(mana, 5)); mana = player.current_mana
    if incoming > 0 and has_dw(player,"庇护") and incoming >= player.current_hp * 0.3:
        bs.cast_bihu(player, min(mana, math.ceil(incoming/4))); mana = player.current_mana
    use_boba = len(alive) >= 2 and has_dw(player, "波及")
    boba_done = False
    while actions > 0 and mana > 0:
        alive = alive_monsters(monsters)
        if not alive: break
        if use_boba and not boba_done and len(alive) >= 2:
            cast_boba(player, monsters, min(len(alive), max(1, mana // 3)))
            mana = player.current_mana; actions -= 1; boba_done = True
        else:
            target = min(alive_monsters(monsters), key=lambda m: m.current_hp)
            x = min(mana, max(1, math.ceil(target.current_hp/2)))
            bs.cast_shaifa(player, target, x, monsters); mana = player.current_mana; actions -= 1


def run_multi_battle(player, monster_defs, rng):
    """多怪战斗，mutate player。返回 {win, paths:[...]}"""
    region = monster_defs[0]["region"] if monster_defs else "扭曲都市"
    state = GameState(); state.current_region = region; state.player = player
    monsters = [bs.make_monster(md) for md in monster_defs]
    state.enemies = monsters
    combat = CombatEngine(state, DiceEngine())
    combat.PROLIFERATION_THRESHOLD = bs.TUNING["PROLIF_THRESHOLD"]
    combat.DEBT_THRESHOLD = bs.TUNING["DEBT_THRESHOLD"]
    for m in monsters:
        combat.init_monster_shards(m)
    combat.reset_monster_activation()  # 进化记录按战斗重置（裁定②接线）
    # 战始：速度复原、清除控场状态
    player.current_speed = player.speed_limit
    player.is_alive = True
    player.status_effects = []
    player.current_mana = 0  # 战始清零；回始再获得等同法限的法力
    activated = {id(m): set() for m in monsters}
    paths_used = []
    max_rounds = 30
    for rnd in range(1, max_rounds+1):
        if not player.is_alive:
            return {"win": False, "paths": paths_used}
        if not alive_monsters(monsters):
            break
        # 回始：获得等同当前法限的法力
        player.current_mana += player.mana_limit
        player.shield = 0
        player.hp_lost_this_round = 0
        for m in monsters:
            m.hp_lost_this_round = 0
            m._jammed = False
            m._nade_minus = 0
        if getattr(player, "tools", None):
            tools_phase(player, monsters, activated, rnd)  # 回始道具阶段（裁定⑬）
        for m in monsters:
            if m.is_alive: bs.monster_round_start(m, activated[id(m)])
            if rnd > 1 and m.is_alive and not getattr(m, "_jammed", False):  # 干扰仪：本回合无法发动道纹
                act = bs.monster_activate(m, activated[id(m)], rng)
                if act and not act.startswith("崩解:"):  # 崩解：激活效果中断
                    bs.apply_control_to_player(act, m, player)
                    if bs.USE_EXCLUSIVE and act in bs.EXCLUSIVE_PRIORITY:
                        bs.apply_exclusive(act, m, player, monsters, rng)
                if bs.sim_maybe_evolve(m, combat):  # 困境进化默认策略（裁定②接线）
                    paths_used.append("进化")
        bs.exclusive_round_start(player, monsters, activated, rng)  # 专属道纹回始（裁定⑨）
        if not player.is_alive:
            return {"win": False, "paths": paths_used}
        # 玩家出手
        player_turn_multi(player, monsters, combat, rng)
        if not alive_monsters(monsters):
            break
        # 所有怪攻击
        for m in monsters:
            if not m.is_alive or not player.is_alive: continue
            must_hit = "必中" in activated[id(m)]
            n_act = bs.get_monster_attack_actions(m, activated[id(m)])
            for i in range(n_act):
                if not player.is_alive: break
                bs.monster_attack_round(m, player, combat, rng, must_hit)
        if not player.is_alive:
            return {"win": False, "paths": paths_used}
        # 回终
        player.shield = 0; player.current_mana = 0
        for m in monsters: m.shield = 0
        bs.exclusive_round_end(player, monsters)  # 专属道纹回终（裁定⑨）
        if not player.is_alive:
            return {"win": False, "paths": paths_used}
        settled = combat.settle_victory_paths()
        for s in settled: paths_used.append(s["type"])
        if not alive_monsters(monsters):
            return {"win": True, "paths": paths_used}
    return {"win": not alive_monsters(monsters) and player.is_alive, "paths": paths_used}


def battle_monster_count(n):
    c = max(1, n - PARAMS["battle_offset"])
    cap = PARAMS.get("cap", 99)
    return min(c, cap)


def run_full_run(rng, pool, region):
    rpool = [m for m in pool if m["region"] == region]
    player = make_run_player()
    player.shards = 20  # 碎片挂在玩家实体上：局内被洗劫/赎金夺取会真实扣减（裁定⑨）
    for n in range(1, 8):
        count = battle_monster_count(n)
        defs = [rng.choice(rpool) for _ in range(count)]
        player.shards = pre_battle_prep(player, player.shards, energy=3, battle_n=n, region=region, rng=rng)
        res = run_multi_battle(player, defs, rng)
        if not res["win"]:
            return {"cleared": False, "reached": n, "paths": res["paths"], "final_hp": player.current_hp}
        # 战终碎片奖励（仅击杀，非杀伐移出）
        for md in defs:
            # 简化：每只击杀怪给 战始血限2%+道纹数5
            player.shards += math.ceil(md["hp"]*0.02) + len(md["dw"])*5
    return {"cleared": True, "reached": 8, "paths": [], "final_hp": player.current_hp}


def measure(runs, pool, region, seed=2026):
    rng = random.Random(seed)
    cleared = 0; reached_dist = {}; path_total = {}
    for _ in range(runs):
        r = run_full_run(rng, pool, region)
        if r["cleared"]: cleared += 1
        reached_dist[r["reached"]] = reached_dist.get(r["reached"], 0) + 1
        for p in r["paths"]: path_total[p] = path_total.get(p,0)+1
    return cleared/runs*100, reached_dist, path_total


def main():
    pool = bs.parse_monsters()
    args = sys.argv[1:]
    if args and args[0] == "all":
        runs = int(args[1]) if len(args) > 1 else 300
        bs.SIM_STATS["evolutions"] = 0; bs.SIM_STATS["collapses"] = 0
        print(f"各副本7场通关率（激进修行策略，每次{runs}局）\n")
        total_cleared = 0
        for region in ["扭曲都市", "罪孽都市", "龙心谷"]:
            cleared = 0; reached = {}
            rng = random.Random(2026)
            for _ in range(runs):
                r = run_full_run(rng, pool, region)
                if r["cleared"]: cleared += 1
                reached[r["reached"]] = reached.get(r["reached"],0)+1
            rate = cleared/runs*100; total_cleared += cleared
            dist = " ".join(f"{('通关' if k==8 else '第'+str(k)+'败')}:{reached.get(k,0)*100//runs}%" for k in range(1,9) if reached.get(k))
            print(f"  {region}: 通关率 {rate:.1f}%  | {dist}")
        print(f"\n  三副本平均通关率: {total_cleared/(runs*3)*100:.1f}%")
        print(f"  [裁定②接线] 进化触发 {bs.SIM_STATS['evolutions']} 次 | 崩解自毁 {bs.SIM_STATS['collapses']} 次（共{runs*3}局）")
        return
    if args and args[0] == "sweep":
        # 扫参找30%：出怪offset × 修行方向
        runs = int(args[1]) if len(args) > 1 else 200
        print(f"扫参 (每次{runs}局)\n{'offset':>6}{'grow':>6} {'通关率':>7}  主死场")
        for off in [2, 3]:
            for grow in ["mix", "speed", "mana"]:
                PARAMS["battle_offset"] = off; PARAMS["grow"] = grow
                rate, dist, _ = measure(runs, pool, "罪孽都市")
                # 主死场
                dead = {k:v for k,v in dist.items() if k!=8}
                top = max(dead, key=dead.get) if dead else "-"
                print(f"{off:>6}{grow:>6} {rate:>6.1f}%  第{top}场" if top!="-" else f"{off:>6}{grow:>6} {rate:>6.1f}%")
        return
    runs = int(args[0]) if args else 300
    region = args[1] if len(args) > 1 else "罪孽都市"
    rate, reached_dist, path_total = measure(runs, pool, region)
    print(f"7场全通关率模拟  怪物池{len(pool)}只  跑{runs}次  offset={PARAMS['battle_offset']} grow={PARAMS['grow']}\n")
    print(f"=== 通关率（7场全清）: {rate:.1f}% ===\n")
    print("最远到达场分布:")
    for n in sorted(reached_dist):
        bar = "█" * int(reached_dist[n]/runs*50)
        tag = "通关" if n==8 else f"第{n}场败"
        print(f"  {tag}: {reached_dist[n]/runs*100:5.1f}%  {bar}")
    print(f"\n路径使用总次数: {path_total}")


if __name__ == "__main__":
    main()
