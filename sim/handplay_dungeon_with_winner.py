#!/usr/bin/env python3
"""用「真实的一阶胜者」真手操乱葬岗（二阶）。

用户要求：测试二阶必须用真实的一阶胜者（从一阶一个个跑出来的胜者快照），
不能用初始角色。本脚本：
1. 加载 data/real_winners/winner_XX.json（真实一阶胜者快照）
2. 以该快照的 player+friends+employees+遗物+碎片 作为起点进入乱葬岗
3. 真手操：每回合显式决策（有【杀伐】则优先使用，否则提交完整普攻；兼顾庇护与理智闪避），
   与 TacticalAI 无关，决策全部来自本脚本的显式 execute_action 调用
4. 输出逐回合战报（数据全部取自引擎返回值）

用法：
    python3 sim/produce_open_world_winners.py --count 3 --max-games 50
    python3 sim/handplay_dungeon_with_winner.py --winner data/real_winners/winner_02.json --battles 1 --seed 7
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from engine.ai_tactics import choose_dodge, choose_attack_target
from sim.monster_targets import (pick_monster_daowen_target,
                                 pick_wave_dodge_targets)  # noqa: E402
from sim.build_learner import _decline_spells, round_start_relic_choices
from sim.produce_real_winners import build_spell_choices
from sim.produce_open_world_winners import validate_open_world_winner_snapshot


_REST_HEAL_PERCENT = {1: 20, 2: 40, 3: 60}
_REST_SHARD_COST = {1: 0, 2: 10, 3: 25}


def _choose_rest_plan(blood_limit: int, current_hp: int, shards: int) -> tuple[int, int]:
    """Choose a valid rest tier and its exact contract allocation.

    The API requires allocation of the tier's full heal amount even if the target
    has less missing HP (actual healing is capped). Prefer the cheapest tier that
    fills the gap; otherwise use the strongest affordable tier and repeat as needed.
    """
    missing_hp = blood_limit - current_hp
    if missing_hp <= 0:
        raise ValueError("rest is only applicable while HP is below the blood limit")

    heal_by_tier = {
        tier: math.ceil(blood_limit * percent / 100)
        for tier, percent in _REST_HEAL_PERCENT.items()
    }
    affordable = [
        tier for tier, cost in _REST_SHARD_COST.items()
        if cost == 0 or shards >= cost
    ]
    covering = [tier for tier in affordable if heal_by_tier[tier] >= missing_hp]
    if covering:
        tier = min(covering, key=lambda candidate: (_REST_SHARD_COST[candidate], candidate))
    else:
        tier = max(affordable, key=lambda candidate: heal_by_tier[candidate])
    return tier, heal_by_tier[tier]


def _prepare_before_battle(e) -> None:
    """Apply bounded pre-battle actions; reject failures instead of retrying forever."""
    prep_action_limit = max(0, int(e.state.energy))
    prep_action_count = 0
    while e.state.energy > 0:
        prep_action_count += 1
        if prep_action_count > prep_action_limit:
            raise RuntimeError("局外行动未消耗精力，已触发安全上限并停止，避免死循环")
        p = e.state.player
        if p and p.current_hp < p.blood_limit:
            tier, heal_amount = _choose_rest_plan(
                p.blood_limit, p.current_hp, e.state.shards)
            hp_before = p.current_hp
            energy_before = e.state.energy
            r = e.execute_action("pre_battle_action", {
                "sub_action": "休整", "tier": tier,
                "heal_allocations": [{"target_ref": "player:0", "amount": heal_amount}]})
            if not r.get("success"):
                raise RuntimeError(f"局外休整{tier}档失败：{r.get('error')}")
            if e.state.energy >= energy_before:
                raise RuntimeError("局外休整成功但精力未减少，已停止避免死循环")
            print(f"局外：休整{tier}档 分配{heal_amount}（实际恢复{p.current_hp - hp_before}）"
                  f"→ {p.current_hp}/{p.blood_limit}")
            continue
        energy_before = e.state.energy
        r = e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1})
        if not r.get("success"):
            raise RuntimeError(f"局外修行1档失败：{r.get('error')}")
        if e.state.energy >= energy_before:
            raise RuntimeError("局外修行成功但精力未减少，已停止避免死循环")
        print("局外：修行1档 → 法限+2")


# 玩家自己回合的手操策略（不用TacticalAI，用最简显式规则）
# 若缺少输出道纹，仍须走两阶段普攻；绝不能把「没有杀伐」当作「跳过行动」。

def _manual_basic_attack(e, log):
    prepared = e.execute_action("prepare_attack", {"actor_ref": "player:0"})
    if not prepared.get("success"):
        log.append(f"  普攻准备失败：{prepared.get('error')}")
        return prepared
    attack = prepared["result"]
    refs = e.combat._combat_entity_refs()
    targets = [option for option in attack["target_options"]
               if option["ref"] in refs and refs[option["ref"]].is_alive]
    if not targets:
        result = {"success": False, "error": "普攻没有合法目标"}
        log.append(f"  普攻未执行：{result['error']}")
        return result
    target_option = min(targets, key=lambda option: refs[option["ref"]].current_hp)
    target = refs[target_option["ref"]]
    per_hit_damage = e.state.player.effective_attack_power()
    hits = [{
        "target_ref": target_option["ref"],
        "damage": per_hit_damage,
        "dodge": False,
        "parry": False,
        "blood_shadow": False,
        "spell_choices": _decline_spells(target_option),
    } for _ in range(attack["hit_count"])]
    resolved = e.execute_action("resolve_attack", {
        "token": attack["token"], "hits": hits,
    })
    if not resolved.get("success"):
        log.append(f"  普攻结算失败：{resolved.get('error')}")
        return resolved
    damage = sum(hit.get("damage_dealt", 0)
                 for hit in resolved.get("result", {}).get("hits", []))
    log.append(f"  普攻{attack['hit_count']}击攻击{target.name} → {damage}伤")
    return resolved


def _pick_monster_daowen(engine, actor):
    """怪物按当前情形择优选道纹——委托 sim.monster_targets 统一入口（2026-08-22）。

    优先级分组表不再在本文件复制（此前多处各抄一份、版本变更后三处仍引用
    已删除的切割/冲击/缓慢）；统一口径见 sim.monster_targets.MONSTER_DAOWEN_*
    （DM裁定2026-08-18：自保→输出→控制→机制，含半血/收割/连续压制修正）。
    本文件仅保留候选过滤与空候选回退的原有语义。
    """
    from sim.monster_targets import pick_monster_daowen_option
    opts = actor["daowen_options"]
    if not opts:
        return None
    m_idx = int(actor["actor_ref"].split(":", 1)[1]) if ":" in actor["actor_ref"] else 0
    enemies = engine.state.enemies
    monster = None
    if 0 <= m_idx < len(enemies):
        monster = enemies[m_idx]
    round_used = (engine.combat._monster_round_used(monster)
                  if monster is not None else set())
    cands = [o for o in opts if o["name"] not in round_used]
    if not cands:
        return None
    p = engine.state.player
    player_low = p is not None and p.is_alive and p.current_hp <= p.blood_limit * 0.5
    monster_low = monster is not None and monster.current_hp <= monster.blood_limit * 0.5
    
    return pick_monster_daowen_option(cands, player_low=player_low,
                                      monster_low=monster_low)


def _monster_daowen_submission(e, actor, option):
    """Build one complete monster-daowen submission for both primary and retry paths."""
    dao = {
        "name": option["name"], "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {
            holder: {spell["spell_name"]: {"use": False} for spell in spells}
            for holder, spells in option.get("trigger_spell_options", {}).items()
        },
    }
    if option["requires_target"]:
        dao["target_ref"] = pick_monster_daowen_target(e, actor["actor_ref"], option)
    if option.get("dodge_submission") == "per_target":
        dao["dodge_targets"] = pick_wave_dodge_targets(option)
    return dao


def _wave_first(choices):
    """Stable-sort wave users before other monsters: phase actions execute in input order.

    A preceding monster can kill a target already frozen in a later wave's prepare
    snapshot. Resolve those per-target choices while their selected targets are alive.
    """
    choices.sort(key=lambda choice: 0 if "dodge_targets" in (choice.get("daowen") or {}) else 1)
    return choices

def manual_player_turn(e, log):
    """满法输出+理智闪避+庇护保命（玩家回合）。返回出手列表。"""
    p = e.state.player
    if p is None or not p.is_alive:
        return []
    out = []
    shielded_this_round = False
    for _ in range(max(1, p.action_count)):
        if not p.is_alive:
            break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            break
        threat = sum(x.attack_count * x.attack_power for x in enemies)
        lethal = threat > p.current_hp + p.shield
        # 1) 保命：仅在真的会被打死、且本回合还没上过盾时上盾
        if (lethal and not shielded_this_round and "庇护" in p.dao_wen
                and p.current_mana >= 2):
            r = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": 2,
                                                "target": p.name,
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                shield = sum(ef.get("amount", 0) for ef in
                             (r.get("execution", {}).get("effects") or []))
                log.append(f"  保命：庇护X=2 → 格挡+{shield}（盾{p.shield}）")
                out.append(r)
                shielded_this_round = True
                continue
        # 2) 输出：优先用【杀伐】；新生成的真实胜者可能没有杀伐，则走正式两阶段普攻。
        if "杀伐" not in p.dao_wen:
            r = _manual_basic_attack(e, log)
            if r.get("success"):
                out.append(r)
                continue
            break
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 2)  # 留2法力给反应法术
        r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                            "target": target.name,
                                            "trigger_spell_choices": {}})
        if r.get("success"):
            dmg = sum(ef.get("actual_damage", 0) for ef in
                      (r.get("execution", {}).get("effects") or []))
            log.append(f"  输出：杀伐X={x} 打{target.name} → {dmg}伤")
            out.append(r)
            continue
        # 失败的法术不得让角色空过本出手机会；改提交有效普攻并保留失败诊断。
        log.append(f"  杀伐未能发动：{r.get('error')}；改用普攻")
        fallback = _manual_basic_attack(e, log)
        if fallback.get("success"):
            out.append(fallback)
            continue
        break
    return out


def _resolve_monster_turn_hand(e, log):
    """怪物阶段：理智闪避 + 反应法术触发（先发制人/后发制人/生生不息）。"""
    prepared = e.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    dodge_budget = 0
    refs = e.combat._combat_entity_refs()
    player_ref = "player:0"
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = _pick_monster_daowen(e, actor)
            if option is not None:
                dao = _monster_daowen_submission(e, actor, option)
                if option["resolves_as"] == "变形":
                    enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                    hit_count = e.state.enemies[enemy_index].attack_power
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
        spell_mana_left = e.state.player.current_mana if e.state.player else 0
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                want_dodge = choose_dodge(e, per_hit, budget_used=dodge_budget)
                if want_dodge:
                    dodge_budget += 1
                    log.append(f"    闪避{per_hit}伤命中（速度-1）")
                hit = {"target_ref": target_ref, "dodge": want_dodge,
                       "blood_shadow": False,
                       "spell_choices": build_spell_choices(target_option, player_ref,
                                                            mana_budget=spell_mana_left)}
                if target_ref == player_ref:
                    eligible = target_option.get("spell_options", {}) or {}
                    for timing in ("before", "after"):
                        for sp in eligible.get(timing, []) or []:
                            if hit["spell_choices"].get(timing, {}).get(sp["spell_name"], {}).get("use"):
                                spell_mana_left = max(0, spell_mana_left - 2)
                if want_dodge and target_option.get("dodge_relic_target_options"):
                    hit["dodge_relic_target_ref"] = target_option["dodge_relic_target_options"][0]["ref"]
                hits.append(hit)
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    result = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": _wave_first(choices)})
    if not result.get("success"):
        primary_error = result.get("error", "未知怪物阶段错误")
        log.append(f"  怪物阶段首提被拒（状态已回滚）：{primary_error}")
        # 兜底：拒绝全部法术，但仍完整提交道纹契约（含波及的dodge_targets）。
        fallback = []
        for actor in prepared["result"]["actors"]:
            dao = None
            hit_count_fb = actor["base_hits_per_attack"]
            if actor["daowen_options"]:
                option = _pick_monster_daowen(e, actor)
                if option is not None:
                    dao = _monster_daowen_submission(e, actor, option)
                    if option["resolves_as"] == "变形":
                        enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                        hit_count_fb = e.state.enemies[enemy_index].attack_power
            target_ref = choose_attack_target(actor["attack_target_options"], refs)
            target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
            attacks = [{"hits": [{
                "target_ref": target_ref, "dodge": False, "blood_shadow": False,
                "spell_choices": _decline_spells(target_option),
            } for _ in range(hit_count_fb)]}
                for _ in range(actor["base_attack_actions"])]
            fallback.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                             "attack_actions": attacks})
        result = e.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"], "choices": _wave_first(fallback)})
        if result.get("success"):
            log.append("  （首提被拒，已按同一token拒绝反应法术后重交成功）")
        else:
            result["primary_error"] = primary_error
            result["error"] = (f"首提失败：{primary_error}；拒绝法术后的回退也失败："
                               f"{result.get('error', '未知怪物阶段错误')}")
            log.append(f"  怪物阶段回退仍被拒：{result.get('error')}")
    # 记录法术触发
    for hit in (result.get("result", {}).get("details") or []):
        for lg in (hit.get("spell_logs") or []):
            if lg.get("used"):
                log.append(f"    法术【{lg['spell']}】→ {lg.get('daowen')}X={lg.get('x')} 对{lg.get('target')}")
    return result


def load_winner(e, snapshot):
    """把真实一阶胜者快照注入乱葬岗引擎（与 tests/test_sealed_candidate_in_dungeon.py 同法）。"""
    player = e._deserialize_entity_full(snapshot["player"])
    e.state.player = player
    for f in snapshot.get("friends", []):
        e.state.friends.append(e._deserialize_entity_full(f))
    for emp in snapshot.get("employees", []):
        e.state.employees.append(e._deserialize_entity_full(emp))
    e.state.shards = snapshot.get("shards", 20)
    e.state.resonance = dict(snapshot.get("resonance") or {})
    for r in snapshot.get("relics", []):
        from engine.models import Relic
        e.state.relics.append(Relic(name=r["name"], effect=r.get("effect", ""),
                                    tags=r.get("tags") or []))
    # 属性点继承
    e.state.attribute_points = snapshot.get("attribute_points", 0)
    e.state.artifacts_owned = list(snapshot.get("artifacts_owned") or [])
    # dragon_traits / first_embrace_traits 是对 relics 的只读视图（按 龙族/血族 tag），
    # 上面复制 relics 时已随 tag 一并带入，无需额外赋值。


def play_dungeon(winner_path: str, battles: int, seed: int):
    with open(winner_path, encoding="utf-8") as f:
        snapshot = json.load(f)
    validate_open_world_winner_snapshot(snapshot)
    p0 = snapshot["player"]
    print("═" * 66)
    print(f"真实一阶胜者进入乱葬岗：{p0['name']} "
          f"血{p0['blood_limit']} 法{p0['mana_limit']} 速{p0['speed_limit']} "
          f"碎片{snapshot.get('shards')}")
    print(f"  道纹：{sorted(p0['dao_wen'])}")
    print(f"  法术：{[s['name'] for s in p0['spells']]}")
    print(f"  朋友：{len(snapshot.get('friends', []))}  遗物：{[r['name'] for r in snapshot.get('relics', [])]}")
    print("═" * 66)

    e = GameEngine(db_path=f"/tmp/hp_{seed}_{os.getpid()}.db", rng_seed=seed,
                   sealed_candidate_path="/tmp/hp_seal.json")
    e.execute_action("setup_attributes", {"name": p0["name"],
                                          "blood_points": 10, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snapshot)

    cleared = 0
    for b in range(1, battles + 1):
        if not e.state.player or not e.state.player.is_alive:
            print(f"\n❌ 第{b}场：轮回者已阵亡，本次乱葬岗挑战结束")
            break
        # 每场战斗前按现存行动休整/修行；已删除的【附煞】不再提交。
        _prepare_before_battle(e)
        print(f"\n──── 第{b}场 · 战始 ────")
        from sim.optional_actions import start_battle
        bs, _art = start_battle(e)
        if not bs.get("success"):
            print("battle_start失败:", bs.get("error"))
            break
        names = list(bs.get("enemies") or [])
        print("出怪：", names)

        won = False
        for rnd in range(1, 30):
            p = e.state.player
            if not p or not p.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            from sim.optional_actions import start_round
            rs, _rsart = start_round(e)
            print(f"\nR{rnd} 回始：玩家 hp={p.current_hp}/{p.blood_limit} 法={p.current_mana} "
                  f"速={p.current_speed} | 敌={[(x.name, x.current_hp) for x in e.state.enemies if x.is_alive]}")
            log = []
            manual_player_turn(e, log)
            for line in log:
                print(line)
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            if not p.is_alive:
                break
            e.execute_action("resolve_ally_phases", {})
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            mp = _resolve_monster_turn_hand(e, log)
            if not mp.get("success"):
                print("怪物阶段失败:", mp.get("error"))
                break
            print(f"  ← 怪物阶段后：玩家 hp={p.current_hp} 法={p.current_mana} "
                  f"敌={[(x.name, x.current_hp) for x in e.state.enemies if x.is_alive]}")
            e.execute_action("round_end", {})
            if not p.is_alive:
                break
        if won and e.state.player and e.state.player.is_alive:
            cleared += 1
            be = e.execute_action("battle_end", {})
            print(f"\n✅ 第{b}场通关！碎片+{be.get('result', {}).get('shard_reward')} "
                  f"→ 共{e.state.shards}")
        else:
            print(f"\n❌ 第{b}场失败（玩家{'阵亡' if not e.state.player or not e.state.player.is_alive else '未清场'}）")
            break
    print(f"\n结果：通关 {cleared}/{battles} 场")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--winner", required=True, help="当前引擎生成的标准一阶胜者快照路径")
    ap.add_argument("--battles", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    play_dungeon(a.winner, a.battles, a.seed)
