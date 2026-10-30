#!/usr/bin/env python3
"""
完整轮回战报生成器：从第1场跑到通关/死亡，记录开局、每场局外行动、遗物、逐回合战斗。
跑多个种子直到出通关局，输出该局完整战报。
"""
import sys, os, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
_spec = importlib.util.spec_from_file_location("bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_sim.py"))
bs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bs)
from engine.models import Entity, GameState, DaoWen, DaoWenInstance, StatusEffect
from engine.combat import CombatEngine
from engine.dice import DiceEngine

POOL = [m for m in bs.parse_monsters() if m["region"] == "罪孽都市"]
REGION = "罪孽都市"

def make_player():
    # 开局：25点→10血/7速/8法 = 60血/14法/8速(出手3)
    p = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
               mana_limit=14, current_mana=14, speed_limit=8, current_speed=8,
               attack_count=1, attack_power=1)
    for n in ["杀伐"]:
        p.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
    return p

def learn(p,n):
    p.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))

def cast_chongji(player, monsters, x):
    """冲击X：耗X法，对所有存活怪造成X伤害（蒙蔽下无效）"""
    if player.current_mana < x: return 0
    player.current_mana -= x
    if player.has_status("蒙蔽"):
        for st in player.status_effects:
            if st.name == "蒙蔽" and st.value > 0:
                st.value -= 1
                if st.value <= 0: player.status_effects.remove(st)
                break
        return 0
    tot = 0
    for m in monsters:
        if m.is_alive and not (m.is_sculptured or m.is_proliferated or m.is_debt_bound):
            m.current_hp = max(0, m.current_hp - x); tot += x
            if m.current_hp <= 0: m.is_alive = False
    return tot

def alive_ms(ms):
    return [m for m in ms if m.is_alive and not (m.is_sculptured or m.is_proliferated or m.is_debt_bound)]

# ===== 带遗物的战斗（逐回合记录到log） =====
def battle(player, defs, log, relics, rng):
    monsters = [bs.make_monster(d) for d in defs]
    state = GameState(); state.current_region = REGION; state.player = player; state.enemies = monsters
    combat = CombatEngine(state, DiceEngine())
    combat.init_monster_shards(monsters[0]) if monsters else None
    for m in monsters: combat.init_monster_shards(m)
    player.current_speed = player.speed_limit; player.is_alive = True; player.status_effects = []
    player.current_mana = 0  # 战始清零；回始再获得等同法限的法力
    activated = {id(m): set() for m in monsters}
    log.append(f"  敌方：{'、'.join(d['name']+'('+str(d['ac'])+'×'+str(d['ap'])+'/'+str(d['hp'])+')' for d in defs)}")
    log.append(f"  贾凡入场：HP{player.current_hp}/{player.blood_limit} 法{player.mana_limit} 速{player.speed_limit}(出手{max(1, player.action_count)})")
    for rnd in range(1, 30):
        if not player.is_alive: log.append(f"  ✗ 贾凡阵亡（第{rnd}回合）"); return False
        if not alive_ms(monsters): break
        player.current_mana += player.mana_limit; player.shield = 0
        # 怪回始+激活
        for m in monsters:
            if m.is_alive:
                bs.monster_round_start(m, activated[id(m)])
                if rnd > 1:
                    act = bs.monster_activate(m, activated[id(m)], rng)
                    if act:
                        if act in ("蒙蔽","坏死","减速","僵化"): bs.apply_control_to_player(act, m, player)
                        log.append(f"    {m.name}道纹出手：激活【{act}{m.dao_wen[act].x_value}】" + (f" 攻击力→{m.attack_power}" if act=="全力" else ""))
        # 玩家出手
        mana = player.current_mana; acts = max(1, player.action_count)
        al = alive_ms(monsters); inc = sum(m.attack_count*m.attack_power for m in al)
        actions_log = []
        lethal = inc >= player.current_hp  # 不防御会死
        # 低血再生
        if player.current_hp <= 20 and mana >= 2 and "再生" in player.dao_wen and not player.has_status("坏死") and acts>0:
            h = bs.cast_zaisheng(player, player, min(mana,5)); actions_log.append(f"再生+{h}HP"); mana=player.current_mana; acts-=1
        # 仅在致命或大伤害时庇护；否则全力击杀
        if (lethal or inc >= player.current_hp*0.5) and acts>0 and "庇护" in player.dao_wen:
            x = min(mana, math.ceil(inc/4)); bs.cast_bihu(player, x); actions_log.append(f"庇护+{player.shield}挡"); mana=player.current_mana; acts-=1
        while acts > 0 and mana > 0 and alive_ms(monsters):
            al = alive_ms(monsters)
            if len(al) >= 3 and "冲击" in player.dao_wen:
                x = min(mana,7); cast_chongji(player, monsters, x)
                actions_log.append(f"冲击{x}(AOE)"); mana=player.current_mana; acts-=1
            else:
                t = min(al, key=lambda m:m.current_hp); x = min(mana, max(1, math.ceil(t.current_hp/2)))
                d = bs.cast_shaifa(player, t, x); actions_log.append(f"杀伐{x}→{t.name}({t.current_hp}残{'倒' if not t.is_alive else ''})"); mana=player.current_mana; acts-=1
        log.append(f"  第{rnd}回合 [回始法力{player.mana_limit}] 贾凡：{'，'.join(actions_log)}")
        if not alive_ms(monsters): log.append("    怪全灭"); break
        # 怪攻击（带避风铃：闪避+3挡）
        for m in monsters:
            if not m.is_alive or not player.is_alive: continue
            n = bs.get_monster_attack_actions(m, activated[id(m)])
            must = "必中" in activated[id(m)]
            for _ in range(n):
                if not player.is_alive: break
                # 手写一轮攻击以便记录避风铃
                for _ in range(m.attack_count):
                    if not player.is_alive: break
                    dmg = m.attack_power
                    if dmg > player.shield and player.current_speed > 0 and not must:
                        player.current_speed -= 1
                        if "避风铃" in relics: player.shield += 3
                        continue
                    if dmg <= 0: continue
                    ab = min(player.shield, dmg); player.shield -= ab
                    player.current_hp = max(0, player.current_hp - (dmg-ab))
                    if player.current_hp <= 0: player.is_alive = False
            log.append(f"    {m.name}攻击×{n}→贾凡HP{player.current_hp} 速{player.current_speed}")
        if "避风铃" in relics and player.current_speed == 0:
            player.shield += 15; log.append(f"    [避风铃]速度归零→+15挡")
        if not player.is_alive:
            log.append(f"  ✗ 贾凡阵亡（第{rnd}回合，HP归零）"); return False
        player.shield = 0; player.current_mana = 0
        for m in monsters: m.shield = 0
        settled = combat.settle_victory_paths()
        for s in settled:
            log.append(f"    ★{s['type']}·{s['monster']}：{s['note']}")
        if settled: return True
    return not alive_ms(monsters) and player.is_alive

def run_one(seed):
    log = []
    rng = random.Random(seed)
    p = make_player()
    shards = 20
    relics = ["守夜灯"]  # 开局发现遗物·守夜灯（钱袋已删除；其效果一度并入第一杯，2026-09-23 重做时废止）
    log.append("【开局】分配25点→10血/7速/8法(60血/14法/8速，出手3)；获20碎片；发现遗物·守夜灯；")
    log.append("        自动获得初始道纹·杀伐；自选残韵·反转；进入一阶副本·罪孽都市。\n")
    for n in range(1, 8):
        count = CombatEngine.monster_spawn_count(n, REGION)
        defs = [rng.choice(POOL) for _ in range(count)]
        log.append(f"━━━━━━ 第{n}场（出怪{count}）━━━━━━")
        # 局外3精力
        prep = []
        if n == 1:
            for dw in ["庇护","再生","冲击"]:
                learn(p,dw); prep.append(f"学习{dw}")
        tiers = [(6,150,"六阶"),(5,100,"五阶"),(4,65,"四阶"),(3,35,"三阶"),(2,15,"二阶"),(1,0,"一阶")]
        e = 3
        while e > 0:
            if p.current_hp < p.blood_limit*0.35 and shards >= 25:
                p.current_hp = min(p.blood_limit, p.current_hp+48); shards-=25; prep.append("休整(25碎)+48"); e-=1
            elif p.current_hp < p.blood_limit*0.2:
                p.current_hp = min(p.blood_limit, p.current_hp+8); prep.append("休整+8"); e-=1
            else:
                if n == 3 and "避风铃" not in relics and shards >= 15:
                    relics.append("避风铃"); shards-=15; prep.append("共鸣·自选避风铃(15碎)"); e-=1
                else:
                    # 激进修行：买能负担的最高档
                    bought = False
                    for pts,cost,name in tiers:
                        if shards >= cost:
                            shards -= cost
                            spd_before = p.speed_limit
                            for _ in range(pts):
                                if p.speed_limit < 5: p.speed_limit += 1
                                else: p.mana_limit += 2
                            p.current_speed = p.speed_limit
                            prep.append(f"修行({name},{cost}碎)+{pts}点→速{p.speed_limit}/法{p.mana_limit}")
                            bought = True; break
                    if not bought: prep.append("修行一阶+1点"); p.speed_limit+=1; p.current_speed=p.speed_limit
                    e-=1
        p.current_mana = p.mana_limit
        log.append(f"  [局外] {'，'.join(prep)}")
        log.append(f"  [战始] 战斗背景：{rng.choice(['帮派巷战','废墟据点','黑市火并'])}")
        won = battle(p, defs, log, relics, rng)
        if not won:
            log.append(f"\n【结局】贾凡于第{n}场阵亡。最远：第{n}场。遗物：{relics}，碎片{shards}")
            return log, False, n
        # 战终：碎片（击杀怪）
        reward = 0; killn = 0
        for d in defs:
            killn += 1  # 简化：本场怪均视为击杀计碎片（雕塑等移出路径不产）
            reward += math.ceil(d["hp"]*0.02) + len(d["dw"])*5
        shards += reward
        log.append(f"  [战终] 碎片+{reward}(基础)→共{shards}；速度复原；精力回3；造物消散。贾凡HP{p.current_hp}/{p.blood_limit}\n")
    log.append(f"【通关】贾凡历经7场，完成一阶罪孽都市！遗物：{relics}，结余碎片{shards}，终态HP{p.current_hp}/{p.blood_limit}")
    return log, True, 8

def main():
    for seed in range(1, 400):
        log, cleared, reached = run_one(seed)
        if cleared:
            print(f"# 完整轮回战报（种子{seed}，通关）\n")
            print("\n".join(log))
            return
    # 没通关则取最深
    best = None
    for seed in range(1, 50):
        log, cleared, reached = run_one(seed)
        if best is None or reached > best[2]:
            best = (log, cleared, reached)
    print(f"# 完整轮回战报（种子，最深第{best[2]}场{'·通关' if best[1] else '·阵亡'}）\n")
    print("\n".join(best[0]))

if __name__ == "__main__":
    main()
