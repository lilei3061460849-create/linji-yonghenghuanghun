#!/usr/bin/env python3
"""Legacy seven-battle combat-only sampler.

This is not the canonical real-winner source after the final-duel rule changed to
last-two-survivors in the open world. Use sim/produce_open_world_winners.py for
current winner evidence. This module remains the shared monster-phase driver and
its old CLI requires an explicit --legacy-seven-battle-mode opt-in.
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI, choose_dodge, choose_attack_target
from sim.monster_targets import pick_monster_daowen_target  # noqa: E402
from sim.build_learner import _decline_spells, _resolve_pending_event, round_start_relic_choices

REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]
WINNER_DIR = os.environ.get(
    "REAL_WINNER_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "real_winners"),
)
STARTER_ATTRIBUTES = {"blood_points": 11, "speed_points": 8, "mana_points": 6}

# 学习顺序：先发制人(法术,免费,需杀伐=起手道纹) → 庇护/再生(道纹,免费)
#          → 生生不息(法术,免费) → 后发制人(法术,免费)
# 但第1场先修行(+2法限)与baseline对齐，成长优先，法术第2场起补学。
SPELL_ORDER = ["先发制人", "生生不息", "后发制人"]
DAOWEN_ORDER = ["庇护", "再生"]
SPELL_REQUIRES = {"先发制人": ["杀伐"], "生生不息": ["再生"], "后发制人": ["庇护"]}

# 每场战斗保留给反应法术的法力（手操会留一手：先发制人反打、后发制人上盾、生生不息回血）
SPELL_MANA_RESERVE = 2



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
    activated = engine.combat._monster_activated.get(id(monster), set()) if monster is not None else set()
    cands = [o for o in opts if o["name"] not in activated]
    if not cands:
        return opts[0]
    p = engine.state.player
    player_low = p is not None and p.is_alive and p.current_hp <= p.blood_limit * 0.5
    monster_low = monster is not None and monster.current_hp <= monster.blood_limit * 0.5
    
    return pick_monster_daowen_option(cands, player_low=player_low,
                                      monster_low=monster_low)

def _spell_step_entry(step: dict, player_ref: str, mana: int) -> dict:
    """为单个法术步骤构造 cycle 内的条目：x 按需推导，target_ref 用 prepare 给的引用。
    自由控X：攻击步骤拿剩余法力（最大化反打），自保/回复步骤留1。"""
    target_ref = step["target_ref"]
    hostile = target_ref != player_ref
    entry = {"x": 1, "target_ref": target_ref}
    if hostile:
        entry["dodge"] = False
        entry["x"] = max(1, mana - 1)  # 攻击步自由控X
    return entry


def build_spell_choices(target_option: dict, player_ref: str, mana_budget: int) -> dict:
    """根据 prepare 给出的 spell_options 构造完整 spell_choices（逐法术显式 use）。

    mana_budget：当前可支配法力（由调用方按命中次数递减），保证法术提交合法。
    """
    spell_options = target_option.get("spell_options", {}) or {}
    out = {}
    for timing in ("before", "after"):
        out[timing] = {}
        for spell in spell_options.get(timing, []) or []:
            name = spell["spell_name"]
            steps = spell.get("steps", [])
            # 决定是否使用
            use = False
            submitted = []
            if timing == "before":
                # 先发制人/借力打力等：有敌对步骤就打；后发制人/庇护类上盾
                has_hostile = any(s.get("target_ref") != player_ref for s in steps)
                if has_hostile:
                    use = True
                elif steps and steps[0].get("daowen") == "庇护":
                    use = True  # 后发制人：上盾
            else:
                # after：生生不息/以牙还牙 —— 掉血后回血，视为总是值得（血量不满才触发）
                use = True
            if use and mana_budget >= 1:
                # 跨步骤递减法力：攻击步拿剩余，自保步留1，避免多步法术超支
                remaining = mana_budget
                cycle = []
                for st in steps:
                    entry = _spell_step_entry(st, player_ref, remaining)
                    cycle.append(entry)
                    remaining -= entry["x"]
                    if remaining < 1:
                        break
                submitted = cycle
            else:
                use = False  # 预算不足时显式拒绝，避免结算法力不足报错
            # 新契约：每个决策槽位一条 steps；老逻辑只算一轮预算，故上限写1
            out[timing][name] = ({"use": True, "steps": submitted, "max_iterations": 1}
                                 if use else {"use": False})
    return out


def _resolve_pending_redemption(engine):
    """Resolve the engine's mandatory redemption interruption before any other action."""
    pending = engine.state.pending_redemption
    if not pending:
        return None
    used = {entity.name for group in
            (engine.state.friends, engine.state.employees, engine.state.temp_friends,
             engine.state.enemies)
            for entity in group}
    if engine.state.player:
        used.add(engine.state.player.name)
    index = 1
    name = f"救赎员工{index}"
    while name in used:
        index += 1
        name = f"救赎员工{index}"
    result = engine.execute_action("resolve_redemption", {"option": 1, "name": name})
    if result.get("success"):
        return result
    return engine.execute_action("resolve_redemption", {"option": 2})


def _has_remaining_enemies(engine) -> bool:
    """Queued waves count as remaining enemies even when the current wave is dead."""
    return bool(
        any(enemy.is_alive for enemy in engine.state.enemies)
        or engine.state.monster_reinforcements
        or engine.state.delayed_monster_reentries
    )


def _resolve_monster_turn_with_spells(engine):
    """怪物阶段解析器：与 build_learner._resolve_monster_turn 相同，
    但攻击玩家时会触发玩家的反应型法术（先发制人/后发制人/生生不息）。"""
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    dodge_budget = 0
    refs = engine.combat._combat_entity_refs()
    player_ref = "player:0"
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                      for sp in spells}
                                             for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = pick_monster_daowen_target(engine, actor["actor_ref"], option)
            if option["dodge_submission"] == "per_target":
                from sim.monster_targets import pick_wave_dodge_targets
                dao["dodge_targets"] = pick_wave_dodge_targets(option)
            # Use the hit count from prepare_monster_phase. Attack power is damage,
            # not the number of hit choices required by the engine.
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
        # 法术法力预算：整个怪物阶段共享（多怪多击会连续触发，逐击递减保证合法）
        spell_mana_left = engine.state.player.current_mana if engine.state.player else 0
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                want_dodge = choose_dodge(engine, per_hit, budget_used=dodge_budget)
                if want_dodge:
                    dodge_budget += 1
                hit = {"target_ref": target_ref, "dodge": want_dodge,
                       "blood_shadow": False,
                       "spell_choices": build_spell_choices(target_option, player_ref,
                                                            mana_budget=spell_mana_left)}
                if target_ref == player_ref:
                    # 每次对玩家的命中若触发法术会消耗法力（杀伐X消耗X、庇护X消耗X）
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
    result = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices})
    if result.get("success"):
        return result
    # 原子失败后兜底：退回不触发法术（与 build_learner 一致），避免把可继续的模拟误记为异常
    fallback = []
    for actor in prepared["result"]["actors"]:
        dao = None
        hit_count_fb = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                      for sp in spells}
                                             for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = pick_monster_daowen_target(engine, actor["actor_ref"], option)
            if option["dodge_submission"] == "per_target":
                from sim.monster_targets import pick_wave_dodge_targets
                dao["dodge_targets"] = pick_wave_dodge_targets(option)
            # Same rule in fallback: preserve prepare_monster_phase's hit count.
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
        attacks = [{"hits": [{
            "target_ref": target_ref, "dodge": False, "blood_shadow": False,
            "spell_choices": _decline_spells(target_option),
        } for _ in range(hit_count_fb)]}
            for _ in range(actor["base_attack_actions"])]
        fallback.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                         "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": fallback})


def choose_pre_battle(e, battle_no, todo_spells, todo_daowen, rng=None):
    """局外行动：先完成免费成长，再用精力雇佣/探索/修行。

    第1场先修行(+2法限)与 baseline 对齐。此后优先学习可用道纹和法术；
    只有免费学习目标处理完毕，才花精力雇佣或探索。旧顺序把罪孽都市的
    雇佣和龙心谷的探索放在学习前，会先耗尽精力，导致角色带着空技能栏开战。
    """
    rng = rng or random.Random()
    p = e.state.player
    if battle_no == 1 and not getattr(p, "_b1_xiuxing", False):
        p._b1_xiuxing = True
        return "修行", {"tier": 1}

    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
    if todo_daowen:
        name = todo_daowen[0]
        if name in REGION_EXCLUSIVE_DAOWEN["罪孽都市"]:
            if not (REGION_EXCLUSIVE_DAOWEN["罪孽都市"] & set(p.dao_wen)):
                todo_daowen.pop(0)
                return choose_pre_battle(e, battle_no, todo_spells, todo_daowen, rng)
        return "学习", {"sub": "daowen", "tier": 1, "names": [name]}

    if todo_spells:
        name = todo_spells[0]
        req = SPELL_REQUIRES.get(name, [])
        if all(required in p.dao_wen for required in req):
            return "学习", {"sub": "spell", "tier": 1, "names": [name]}
        todo_spells.pop(0)
        return choose_pre_battle(e, battle_no, todo_spells, todo_daowen, rng)

    # 免费成长已完成，现在才用精力建立副本策略。
    # 罪孽都市：雇佣高攻员工当靶子，让怪物优先攻击员工替轮回者抗伤。
    if e.state.current_region == "罪孽都市" and len(e.state.employees) < 3:
        return "雇佣", {"name": f"铁卫{len(e.state.employees) + 1}",
                        "blood_alloc": 5, "atk_bundles": 5}
    # 龙心谷：免费成长完成后探索，争取取得带保护道纹的朋友。
    if e.state.current_region == "龙心谷" and not e.state.friends:
        return "探索", {"tier": 1}
    if p and p.current_hp < p.blood_limit * 0.6:
        return "休整", {"tier": 1, "heal_allocations": [
            {"target_ref": "player:0", "amount": 8}]}
    return "修行", {"tier": 1}


def unlock_sin_city_daowen(e, log=None):
    """罪孽都市一阶：用残韵对怪物的专属道纹转化，让玩家获得第一种罪孽都市道纹。

    门禁：学习罪孽都市专属道纹须先经残韵获得一种（规则正文）。优先 点金→转换→典当
    （2026-10-13：【逼债】删除，【典当】顶替其闭环格；解锁门禁本身不挑道纹）；
    无点金怪则用任意专属道纹+存在的残韵路径解锁门禁。返回是否成功解锁。"""
    p = e.state.player
    if p is None or not p.is_alive:
        return False
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
    from engine.daowen import ResonanceEngine
    exclusive = REGION_EXCLUSIVE_DAOWEN["罪孽都市"]
    if exclusive & set(p.dao_wen):
        return True  # 已解锁
    stock = {k: v for k, v in e.state.resonance.items() if v > 0}
    if not stock:
        return False

    # 第一优先：点金怪 → 转换 → 典当（解锁门禁即可，不挑道纹）
    if "转换" in stock:
        for m in e.state.enemies:
            if m.is_alive and "点金" in m.dao_wen:
                r = e.execute_action("use_resonance", {
                    "source_daowen": "点金", "resonance_type": "转换",
                    "target_ref": f"enemy:{e.state.enemies.index(m)}"})
                if r.get("success"):
                    if log is not None:
                        log.append(f"  残韵：转换 点金 → 典当（现持有{list(p.dao_wen)}）")
                    return True
    # 兜底：任意专属道纹 × 存在路径的残韵类型
    for m in e.state.enemies:
        if not m.is_alive:
            continue
        for src in list(m.dao_wen):
            if src not in exclusive:
                continue
            for rtype in ("转换", "反转", "曲解"):
                if stock.get(rtype, 0) <= 0:
                    continue
                if ResonanceEngine.find_transformation(src, rtype) is None:
                    continue
                r = e.execute_action("use_resonance", {
                    "source_daowen": src, "resonance_type": rtype,
                    "target_ref": f"enemy:{e.state.enemies.index(m)}"})
                if r.get("success"):
                    if log is not None:
                        log.append(f"  残韵：{rtype} {src} → 解锁罪孽都市道纹（现持有{list(p.dao_wen)}）")
                    return True
    return False


def play_first_tier(seed: int, region: str, sealed_path: str,
                    db_path: str = None, telemetry: dict = None,
                    debt_build: bool = False, spend_shards: bool = True) -> dict:
    """跑一局一阶轮回，通关后把封存快照写到 sealed_path。

    debt_build=True：罪孽都市专属构建——残韵解锁门禁并学习【赎金】
    （2026-10-13：【逼债】删除，债务武器换成赎金抽碎片）。
    spend_shards=True（默认）：七场局外把碎片尽量花成战力（高阶修行/附煞/
    共鸣遗物），封存带"已转化战力"而非"剩余碎片"——不花碎片怎么快速提高战力？
    不提高战力怎么通过战斗？（用户裁定：花碎片是正常玩法，设为默认）"""
    rng = random.Random(seed)
    db_path = db_path or f"/tmp/winner_{seed}_{os.getpid()}_{random.random():.6f}.db"
    e = GameEngine(db_path=db_path, rng_seed=seed,
                   sealed_candidate_path=sealed_path)
    e.execute_action("setup_attributes", {
        "name": "贾凡", **STARTER_ATTRIBUTES,
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    # 2026-10-13：【三相残韵盘】删除，无需规避的可选战始遗物。
    optional_relics = set()
    starter_relic = next((n for n in setup["result"]["relic_choices"] if n not in optional_relics),
                         setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": starter_relic})

    ai = TacticalAI(e)
    todo_spells = list(SPELL_ORDER)
    todo_daowen = list(DAOWEN_ORDER)
    if debt_build:
        todo_daowen.append("赎金")
    cleared = 0

    for b in range(1, 8):
        while e.state.energy > 0:
            # 花碎片→战力（spend_shards）：**先学完免费法术/道纹，再花碎片修行**。
            # 顺序错误会让 spend 把精力花光、免费法术学不上（胜者法术为空的bug）。
            if todo_spells or todo_daowen:
                act, params = choose_pre_battle(e, b, todo_spells, todo_daowen, rng)
                r = e.execute_action("pre_battle_action", {"sub_action": act, **params})
                if r.get("success"):
                    if act == "学习" and params.get("sub") == "spell":
                        learned = r["result"].get("spells") or []
                        for sp in learned:
                            if sp["name"] in todo_spells:
                                todo_spells.remove(sp["name"])
                    if act == "学习" and params.get("sub") == "daowen":
                        for nm in r["result"].get("names", []):
                            if nm in todo_daowen:
                                todo_daowen.remove(nm)
                    # 雇佣后：选择发现的转化道纹（否则引擎阻塞后续行动导致死循环）
                    if act == "雇佣" and e.state.pending_daowen_choices:
                        emp_name = next(iter(e.state.pending_daowen_choices))
                        choices = e.state.pending_daowen_choices[emp_name]
                        prefer = next((d for d in choices
                                       if d in ("蒙蔽", "全速", "弱化", "愤怒", "衰败", "坠落")),
                                      choices[0])
                        e.execute_action("choose_hired_daowen", {"name": emp_name, "daowen": prefer})
                    if e.state.pending_relic_choices:
                        e.execute_action("choose_discovered_relic",
                                         {"relic_name": e.state.pending_relic_choices[0]})
                    if e.state.pending_item_choices:
                        e.execute_action("choose_discovered_item",
                                         {"item_name": e.state.pending_item_choices[0]})
                    if e.event_pool.current is not None:
                        _resolve_pending_event(e)
                    continue
                # 学习失败（门禁/碎片不足）：避免死循环，兜底修行1档
                e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": 1})
                continue
            # 花碎片→战力：修行高阶（1点法限/档，越贵越省精力）> 附煞冥煞 > 共鸣2档遗物。
            if spend_shards and e.state.shards >= 35 and e.state.player:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": 3,
                    "allocations": {"speed_points": 0, "mana_points": 3}})
                if r.get("success"):
                    continue
            if spend_shards and e.state.shards >= 25 and e.state.player:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": "杀伐"})
                if r.get("success"):
                    continue
            if spend_shards and e.state.shards >= 15 and e.state.player:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": 2,
                    "allocations": {"speed_points": 0, "mana_points": 2}})
                if r.get("success"):
                    continue
            if spend_shards and e.state.shards >= 15 and e.state.player:
                r = e.execute_action("pre_battle_action", {"sub_action": "共鸣", "tier": 2})
                if r.get("success") and e.state.pending_relic_choices:
                    e.execute_action("choose_discovered_relic",
                                     {"relic_name": e.state.pending_relic_choices[0]})
                    continue
            act, params = choose_pre_battle(e, b, todo_spells, todo_daowen, rng)
            r = e.execute_action("pre_battle_action", {"sub_action": act, **params})
            if r.get("success"):
                if act == "学习" and params.get("sub") == "spell":
                    learned = r["result"].get("spells") or []
                    for sp in learned:
                        if sp["name"] in todo_spells:
                            todo_spells.remove(sp["name"])
                if act == "学习" and params.get("sub") == "daowen":
                    for nm in r["result"].get("names", []):
                        if nm in todo_daowen:
                            todo_daowen.remove(nm)
                # 雇佣后：从发现的转化道纹中选输出/控制类（提高员工威胁分）
                if act == "雇佣" and e.state.pending_daowen_choices:
                    emp_name = next(iter(e.state.pending_daowen_choices))
                    choices = e.state.pending_daowen_choices[emp_name]
                    prefer = next((d for d in choices
                                   if d in ("蒙蔽", "全速", "弱化", "愤怒", "衰败", "坠落")),
                                  choices[0])
                    e.execute_action("choose_hired_daowen", {"name": emp_name, "daowen": prefer})
                if e.state.pending_relic_choices:
                    e.execute_action("choose_discovered_relic",
                                     {"relic_name": e.state.pending_relic_choices[0]})
                if e.state.pending_item_choices:
                    e.execute_action("choose_discovered_item",
                                     {"item_name": e.state.pending_item_choices[0]})
                if e.event_pool.current is not None:
                    _resolve_pending_event(e)
                if not e.state.player or not e.state.player.is_alive:
                    return {"cleared": cleared, "won": False, "invalid": False}
            else:
                if e.state.energy >= e.state.energy + 0:  # 失败已退精力，兜底修行
                    e.execute_action("pre_battle_action",
                                     {"sub_action": "修行", "tier": 1})

        active_relics = {relic.name for relic in e.state.relics}
        from sim.optional_actions import start_battle as _sb
        bs, _bs_artifacts = _sb(e)
        if not bs.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_start: {bs.get('error')}"}

        for _ in range(40):
            if not e.state.player or not e.state.player.is_alive:
                break
            if not _has_remaining_enemies(e):
                break
            from sim.optional_actions import start_round as _sr
            rs, _rs_artifacts = _sr(e)
            if not rs.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"round_start: {rs.get('error')}"}
            redemption = _resolve_pending_redemption(e)
            if redemption is not None and not redemption.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"redemption after round_start: {redemption.get('error')}"}
            # 罪孽都市专属：战斗内先用残韵解锁门禁（获得第一种专属道纹后局外才能学赎金）
            if debt_build:
                unlock_sin_city_daowen(e)
                redemption = _resolve_pending_redemption(e)
                if redemption is not None and not redemption.get("success"):
                    return {"cleared": cleared, "won": False, "invalid": True,
                            "reason": f"redemption after resonance: {redemption.get('error')}"}
            # 玩家回合：保留法术法力（手操留一手），其余满法输出
            p = e.state.player
            actual_mana = p.current_mana
            reserve = min(SPELL_MANA_RESERVE, max(0, actual_mana - 1))
            if reserve > 0:
                p.current_mana = actual_mana - reserve  # 让AI只看到可花部分
            ai.new_round()
            try:
                ai.take_turn()
            except Exception as ex:
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"combat: {type(ex).__name__}: {ex}"}
            # 把保留法力还给玩家，供怪物阶段反应法术使用
            if reserve > 0:
                p.current_mana = min(p.mana_limit, p.current_mana + reserve)
            redemption = _resolve_pending_redemption(e)
            if redemption is not None and not redemption.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"redemption after player action: {redemption.get('error')}"}
            if not e.state.player or not e.state.player.is_alive:
                break
            if not _has_remaining_enemies(e):
                break
            e.execute_action("resolve_ally_phases", {})
            redemption = _resolve_pending_redemption(e)
            if redemption is not None and not redemption.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"redemption after ally phase: {redemption.get('error')}"}
            if not _has_remaining_enemies(e):
                break
            mp = _resolve_monster_turn_with_spells(e)
            if not mp.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"monster_phase: {mp.get('error')}"}
            redemption = _resolve_pending_redemption(e)
            if redemption is not None and not redemption.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"redemption after monster phase: {redemption.get('error')}"}
            if mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})
            redemption = _resolve_pending_redemption(e)
            if redemption is not None and not redemption.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"redemption after round_end: {redemption.get('error')}"}

        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared, "won": False, "invalid": False}
        if _has_remaining_enemies(e):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": "round limit reached with living enemies or pending reinforcements"}
        redemption = _resolve_pending_redemption(e)
        if redemption is not None and not redemption.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"redemption before battle_end: {redemption.get('error')}"}
        ended = e.execute_action("battle_end", {})
        if not ended.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_end: {ended.get('error')}"}
        crown = ended.get("result", {}).get("final_crown", {})
        outcome = crown.get("outcome")
        # 先看冠冕：无候选时【封存】会重置状态（player=None），必须在阵亡检查前判定
        if outcome == "sealed":
            return {"cleared": cleared + 1, "won": True, "invalid": False,
                    "sealed": True, "sealed_name": crown.get("sealed_name")}
        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared + 1, "won": False, "invalid": False}
        cleared += 1

        if outcome == "duel_start" or e.state.in_final_duel:
            # 第8场死斗：用修复后的PvP对称驱动（守擂方走玩家侧接口：法力制/
            # 出手次数按轮回者 action_count（当前固定2次）/自由控X，勿当怪物处理——用户裁定死斗必须按PvP规则）。
            from sim.duel_pvp import run_duel_pvp
            def _act():
                """挑战者死斗：专注杀守擂主将（轮回者）——与守擂方专注策略对称。
                TacticalAI 会分散输出（保命/控场），死斗胜率仅1/6；专注杀主将
                才能与守擂方五五开（死斗只允许一名轮回者离开：主将死即败）。"""
                p = e.state.player
                if not p or not p.is_alive:
                    return False
                lord = next((x for x in e.state.enemies
                             if x.is_alive and x.entity_type == "轮回者"), None)
                if lord is None:
                    return False
                x = max(1, p.current_mana - 3)
                if x < 1:
                    return False
                r = e.execute_action("use_daowen", {
                    "daowen_name": "杀伐", "x": x,
                    "target_ref": f"enemy:{e.state.enemies.index(lord)}",
                    "trigger_spell_choices": {}})
                return bool(r.get("success"))
            dr = run_duel_pvp(e, _act, max_rounds=60, max_steps=400)
            duel_won = dr.get("winner") == "challenger"
            if not duel_won or not e.state.player or not e.state.player.is_alive:
                return {"cleared": cleared, "won": False, "invalid": False,
                        "duel": "lost", "duel_reason": dr.get("reason")}
            dr = e.execute_action("resolve_final_duel", {"outcome": "victory"})
            if not dr.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"resolve_final_duel: {dr.get('error')}"}
            if dr.get("result", {}).get("pending_terminal_choice"):
                options = dr["result"].get("options") or []
                if options:
                    ca = e.execute_action("choose_terminal_artifact", {"choice": 1})
                    if not ca.get("success"):
                        return {"cleared": cleared, "won": False, "invalid": True,
                                "reason": f"choose_terminal_artifact: {ca.get('error')}"}
                    if e.state.pending_first_embrace:
                        e.execute_action("choose_first_embrace", {"choice": 1})
            return {"cleared": cleared, "won": True, "invalid": False,
                    "duel_won": True, "terminal_artifact": True}
    return {"cleared": cleared, "won": True, "invalid": False}


def extract_sealed_snapshot(seal_data: dict, tier: int) -> dict:
    """Unwrap the engine's canonical per-tier seal queue and return this run's character snapshot.

    The engine persists candidates as {candidates: {tier: [full_character_snapshot]}}.
    That container is not itself a winner sample and must never be copied into winner_XX.json.
    """
    if isinstance(seal_data, dict) and isinstance(seal_data.get("player"), dict):
        if "candidates" in seal_data:
            raise ValueError("ambiguous seal data contains both player and candidates")
        return seal_data
    if not isinstance(seal_data, dict):
        raise ValueError("sealed candidate file is not a JSON object")
    queues = seal_data.get("candidates")
    if not isinstance(queues, dict):
        raise ValueError("sealed candidate file has no candidates queue")
    queue = queues.get(str(tier), queues.get(tier))
    if not isinstance(queue, list) or len(queue) != 1:
        raise ValueError(f"expected exactly one newly sealed tier-{tier} candidate")
    snapshot = queue[0]
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("player"), dict):
        raise ValueError("sealed candidate queue does not contain a full character snapshot")
    return snapshot


def validate_winner_snapshot(snapshot: dict) -> None:
    """Reject legacy wrappers and snapshots without a verifiable seven-battle origin."""
    if not isinstance(snapshot, dict) or "candidates" in snapshot:
        raise ValueError("legacy candidates wrapper is not a real winner snapshot")
    player = snapshot.get("player")
    origin = snapshot.get("origin") or {}
    if not isinstance(player, dict) or player.get("entity_type") != "轮回者":
        raise ValueError("winner snapshot is missing its top-level reincarnator")
    if not isinstance(origin.get("seed"), int) or not origin.get("region"):
        raise ValueError("winner snapshot is missing seed/region provenance")
    if origin.get("generator") != "sim/produce_real_winners.py":
        raise ValueError("winner snapshot has no recognized generator provenance")
    if origin.get("cleared") != 7:
        raise ValueError("winner snapshot did not clear all seven first-tier battles")
    attrs = origin.get("initial_attributes")
    if (not isinstance(attrs, dict)
            or sum(int(value) for value in attrs.values()) != 25
            or origin.get("initial_attribute_points") != 25):
        raise ValueError("winner snapshot does not record a valid 25-point initial build")
    if not origin.get("sealed"):
        raise ValueError("winner snapshot was not sealed by the final-crown flow")
    if player.get("is_alive") is False:
        raise ValueError("winner snapshot contains a dead reincarnator")


def winner_summary(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    p = d.get("player") or {}
    return {
        "name": p.get("name"), "血": p.get("blood_limit"), "法": p.get("mana_limit"),
        "速": p.get("speed_limit"), "攻": f"{p.get('attack_count')}×{p.get('attack_power')}",
        "道纹": sorted(p.get("dao_wen", {})),
        "法术": [s.get("name") for s in p.get("spells", [])],
        "朋友": [(fr.get("name"), fr.get("attack_count"), fr.get("attack_power"))
                for fr in d.get("friends", [])],
        "员工": len(d.get("employees", [])),
        "碎片": d.get("shards"),
        "遗物": [r.get("name") for r in d.get("relics", [])],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-seven-battle-mode", action="store_true",
                    help="显式运行旧七场战斗采样器；结果不能作为当前开放世界胜者证据")
    ap.add_argument("--count", type=int, default=3, help="需要的旧式战斗胜者数量")
    ap.add_argument("--max-games", type=int, default=500, help="最多跑多少局")
    ap.add_argument("--no-spend", action="store_true",
                    help="旧式七场局外不花碎片提升战力（对照实验用）")
    args = ap.parse_args()
    if not args.legacy_seven_battle_mode:
        print("旧七场战斗采样器已停用。请使用 sim/produce_open_world_winners.py。")
        raise SystemExit(2)

    os.makedirs(WINNER_DIR, exist_ok=True)
    outcome_counts: dict[str, int] = {}
    clear_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}
    sample_hashes: list[dict[str, str]] = []
    invalid_reasons: dict[str, int] = {}
    # 真实胜者必须可累积：绝不清空已有样本。
    # 文件编号与随机种子都从现存记录之后继续，避免覆盖或重复跑同一批种子。
    existing_indices = []
    existing_seeds = []
    for fn in os.listdir(WINNER_DIR):
        match = re.fullmatch(r"winner_(\d+)\.json", fn)
        if not match:
            continue
        existing_indices.append(int(match.group(1)))
        path = os.path.join(WINNER_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            origin = existing.get("origin") or {}
            if isinstance(origin.get("seed"), int):
                existing_seeds.append(origin["seed"])
        except (OSError, json.JSONDecodeError):
            # 不覆盖损坏/旧格式文件；由后续独立审计决定是否剔除。
            pass

    winners = 0
    next_index = max(existing_indices, default=0) + 1
    seed = max(existing_seeds, default=0) + 1
    played = 0
    last_out = None
    while winners < args.count and played < args.max_games:
        region = REGIONS[seed % len(REGIONS)]
        sealed_path = f"/tmp/winner_seal_{seed}.json"
        if os.path.exists(sealed_path):
            os.remove(sealed_path)
        r = play_first_tier(seed, region, sealed_path, spend_shards=not args.no_spend)
        played += 1
        outcome_key = ("won" if r.get("won") else
                       "invalid" if r.get("invalid") else
                       "duel_lost" if r.get("duel") == "lost" else "died_or_stalled")
        outcome_counts[outcome_key] = outcome_counts.get(outcome_key, 0) + 1
        clear_key = str(r.get("cleared", 0))
        clear_counts[clear_key] = clear_counts.get(clear_key, 0) + 1
        region_counts[region] = region_counts.get(region, 0) + 1
        if r.get("reason"):
            reason_key = str(r["reason"])
            invalid_reasons[reason_key] = invalid_reasons.get(reason_key, 0) + 1
        tag = "✅通关" if r.get("won") else "❌阵亡"
        extra = ""
        if r.get("won") and os.path.exists(sealed_path):
            out = os.path.join(WINNER_DIR, f"winner_{next_index:02d}.json")
            with open(sealed_path, encoding="utf-8") as f:
                seal_data = json.load(f)
            from engine.gamedata import REGION_TIERS
            tier = int(REGION_TIERS.get(region, 1))
            snapshot = extract_sealed_snapshot(seal_data, tier)
            # 来源字段来自本次真实引擎运行；不允许旧包装结构被计入本批胜者。
            snapshot["origin"] = {
                "region": region,
                "seed": seed,
                "rank": next_index,
                "cleared": r.get("cleared"),
                "sealed": bool(r.get("sealed")),
                "duel_won": r.get("duel_won", False),
                "terminal_artifact": r.get("terminal_artifact", False),
                "sealed_name": r.get("sealed_name"),
                "generator": "sim/produce_real_winners.py",
                "generator_version": 2,
                "initial_attributes": dict(STARTER_ATTRIBUTES),
                "initial_attribute_points": sum(STARTER_ATTRIBUTES.values()),
            }
            validate_winner_snapshot(snapshot)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            with open(out, "rb") as f:
                sample_hashes.append({
                    "file": os.path.basename(out),
                    "sha256": hashlib.sha256(f.read()).hexdigest(),
                    "seed": str(seed),
                    "region": region,
                })
            winners += 1
            last_out = out
            next_index += 1
            s = winner_summary(out)
            extra = "  " + " ".join(f"{k}{v}" for k, v in s.items())
        elif r.get("won"):
            extra = "（封存槽冲突）"
        print(f"#{played:>3} seed={seed:<4} {region} 通关{ r['cleared']}/7 {tag}{extra}")
        seed += 1

    report = {
        "schema_version": 1,
        "generator": "sim/produce_real_winners.py",
        "generator_version": 2,
        "requested_winners": args.count,
        "max_games": args.max_games,
        "games_played": played,
        "winners_generated_this_run": winners,
        "starter_attributes": dict(STARTER_ATTRIBUTES),
        "starter_attribute_points": sum(STARTER_ATTRIBUTES.values()),
        "outcome_counts": outcome_counts,
        "cleared_battle_counts": clear_counts,
        "region_counts": region_counts,
        "invalid_reasons": invalid_reasons,
        "samples": sample_hashes,
        "existing_sample_files_seen": sorted(
            fn for fn in os.listdir(WINNER_DIR) if re.fullmatch(r"winner_\d+\.json", fn)
        ),
    }
    report_path = os.path.join(WINNER_DIR, "sampling_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n完成：{played} 局跑出 {winners} 位真实一阶胜者 → {WINNER_DIR}/")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if winners and last_out:
        print(f"本批最后一位胜者：{os.path.basename(last_out)}")
        s = winner_summary(last_out)
        for k, v in s.items():
            print(f"  {k}: {v}")


    # A green workflow must mean the requested number of genuine winners exists.
    # Do not let a 500-game run with zero winners masquerade as successful sampling.
    if args.count > 0 and winners < args.count:
        print(f"错误：要求 {args.count} 位真实胜者，实际仅生成 {winners} 位。")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
