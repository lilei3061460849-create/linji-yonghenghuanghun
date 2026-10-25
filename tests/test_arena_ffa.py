"""养蛊场（怪物互斗训练模式）的引擎闸门与失败品判据。

覆盖三件事：
  1. 默认关闭：怪物阶段的合法目标仍然只有玩家侧，怪物之间不能互相攻击——正式玩法不受影响；
  2. 打开 `state.arena_ffa`：怪物之间可以互相攻击，并且允许逐个 actor 提交（严格交替）；
  3. 「自己行动导致自己死亡＝失败品」：训练器把它记为 self_kill 且适应度 0。
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))

import monster_arena as arena  # noqa: E402
from engine.models import StatusEffect  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402


def _lineup(n: int = 2) -> list[dict]:
    """养蛊场出手/自由行动测试要求每只参赛怪至少有一条现行可发动道纹。"""
    DaoWenEngine.register_all()
    eligible = [d for d in arena.monster_pool()
                if any(name in DaoWenEngine._registry for name in d.get("dao_wen", []))]
    return [dict(d) for d in eligible[:n]]


# 2026-10-25：怪物攻击并入玩家同一套面板后，击数＝[攻击次数]＝当前速度。
# 这里统一用 X=1 发动候选道纹：满 X 发动的【变形】会把法力付光再与速度互换，
# 结果当回合速度归零、可出手击数变成 0（那是真实后果，不是本文件要测的东西）。
def _prepare(e):
    prep = e.execute_action("prepare_monster_phase", {})
    assert prep["success"], prep.get("error")
    return prep["result"]


def test_default_battle_attack_targets_are_player_side_only():
    """默认（闸门关闭）：怪物的**攻击**合法目标只有玩家侧，且真的打不到别的怪物。

    道纹的 target_options 不在此断言：闸门关闭时该分支走的是改动前逐字节相同的旧代码，
    旧行为本来就允许怪物道纹把其它怪物列为可选目标（属既有语义，本次未动）。
    """
    e = arena.build_arena(_lineup(2))
    e.state.arena_ffa = False
    res = _prepare(e)
    assert res["actors"], "应当有可行动怪物"
    for actor in res["actors"]:
        refs = {t["ref"] for t in actor["attack_target_options"]}
        assert "player:0" in refs
        assert not any(r.startswith("enemy:") for r in refs), \
            "闸门关闭时怪物不得把其它怪物列为攻击目标"

    # 行为面复核：硬把攻击目标顶成另一只怪物，必须被拒
    actor = res["actors"][0]
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    daowen = None
    if actor["daowen_options"] and not actor["daowen_options"][0].get("requires_target"):
        opt = actor["daowen_options"][0]
        daowen = {"name": opt["name"], "x": 1, "dodge": False, "blood_shadow": False}
    rogue = {"actor_ref": actor["actor_ref"], "daowen": daowen,
             "attack_actions": [{"hits": [dict(hit, target_ref="enemy:1")
                                          for _ in range(max(1, actor["base_hits_per_attack"]))]}
                                for _ in range(max(1, actor["base_attack_actions"]))]}
    out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [rogue]})
    assert not out["success"], "闸门关闭时怪物攻击另一只怪物必须被引擎拒绝"


def test_arena_ffa_lets_monsters_fight_each_other():
    """打开闸门：怪物可以把其它怪物列为目标，并且真的能打掉对方的生命。"""
    e = arena.build_arena(_lineup(2))
    assert e.state.arena_ffa is True
    res = _prepare(e)
    actor = next(a for a in res["actors"] if a["actor_ref"] == "enemy:0")
    rival_refs = {t["ref"] for t in actor["attack_target_options"]}
    assert "enemy:1" in rival_refs, "养蛊场里 0 号怪必须能把 1 号怪列为攻击目标"
    assert "player:0" not in rival_refs, "观众（玩家侧）不参战，不该出现在目标里"

    rival = e.state.enemies[1]
    hp_before = rival.current_hp
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    choice = {"actor_ref": "enemy:0", "daowen": None,
              "attack_actions": [{"hits": [dict(hit, target_ref="enemy:1")
                                           for _ in range(max(1, actor["base_hits_per_attack"]))]}
                                 for _ in range(max(1, actor["base_attack_actions"]))]}
    if actor["daowen_options"]:      # 有合法道纹时必须提交一个（引擎契约）
        opt = actor["daowen_options"][0]
        choice["daowen"] = {"name": opt["name"], "x": 1,
                            "dodge": False, "blood_shadow": False}
        if opt.get("requires_target"):
            choice["daowen"]["target_ref"] = "enemy:1"
    out = e.execute_action("resolve_monster_phase",
                           {"token": res["token"], "choices": [choice]})
    assert out["success"], out.get("error")
    assert e.state.enemies[1].current_hp < hp_before, "养蛊场里怪物应当能打掉对手生命"


def test_arena_ffa_allows_single_actor_submission():
    """养蛊场允许逐个 actor 提交（严格交替）；闸门关闭时仍要求一次交齐全部 actor。"""
    e = arena.build_arena(_lineup(2))
    res = _prepare(e)
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    actor = res["actors"][0]
    daowen = None
    if actor["daowen_options"]:
        opt = actor["daowen_options"][0]
        daowen = {"name": opt["name"], "x": 1,
                  "dodge": False, "blood_shadow": False}
        if opt.get("requires_target"):
            daowen["target_ref"] = actor["attack_target_options"][0]["ref"]
    one = {"actor_ref": actor["actor_ref"], "daowen": daowen,
           "attack_actions": [{"hits": [dict(hit, target_ref=actor["attack_target_options"][0]["ref"])
                                        for _ in range(max(1, actor["base_hits_per_attack"]))]}
                              for _ in range(max(1, actor["base_attack_actions"]))]}
    ok = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [one]})
    assert ok["success"], f"养蛊场应当接受单 actor 提交: {ok.get('error')}"
    assert e.state.combat_subphase == "player_actions", "还有怪物没出手，子阶段应退回玩家行动阶段"

    # 闸门关闭时：同一种单 actor 提交必须被拒（要求一次交齐）
    e2 = arena.build_arena(_lineup(2))
    e2.state.arena_ffa = False
    res2 = _prepare(e2)
    a2 = res2["actors"][0]
    d2 = None
    if a2["daowen_options"] and not a2["daowen_options"][0].get("requires_target"):
        d2 = {"name": a2["daowen_options"][0]["name"], "x": a2["daowen_options"][0].get("x") or 1,
              "dodge": False, "blood_shadow": False}
    one2 = {"actor_ref": a2["actor_ref"], "daowen": d2,
            "attack_actions": [{"hits": [dict(hit, target_ref="player:0")
                                         for _ in range(max(1, a2["base_hits_per_attack"]))]}
                               for _ in range(max(1, a2["base_attack_actions"]))]}
    out2 = e2.execute_action("resolve_monster_phase", {"token": res2["token"], "choices": [one2]})
    assert not out2["success"], "闸门关闭时必须要求一次提交全部 actor"


def test_ffa_self_kill_is_scored_zero():
    """自己行动导致自己死亡（如【凡庸】空转自爆）＝失败品，适应度 0。"""
    res = arena.run_match(_lineup(2), dict(arena.DEFAULT_WEIGHTS), seed=0, max_rounds=7,
                          setup=lambda e: (
                              setattr(e.state.enemies[0], "mana_limit", 0),
                              setattr(e.state.enemies[0], "current_mana", 0),
                              setattr(e.state.enemies[0], "blood_limit", 5_000),
                              setattr(e.state.enemies[0], "current_hp", 5_000)))
    assert res["stats"][0]["self_kill"] is True, "连续 0 伤空转应当被【凡庸】判定为失败品"
    assert arena.fitness(res, 0, dict(arena.DEFAULT_WEIGHTS)) == 0.0


def _free_env(n=2):
    e = arena.build_arena(_lineup(n), free_actions=True)
    res = _prepare(e)
    return e, res


def _hit(ref):
    return {"dodge": False, "blood_shadow": False,
            "spell_choices": {"before": {}, "after": {}}, "target_ref": ref}


def test_free_actions_allows_double_attack_double_daowen_and_mixed():
    """行动自由化（2026-10-03 用户裁定）：攻击×2 / 道纹×2 / 一攻一道纹 都必须可结算。"""
    # 攻击×2：同回合两组命中都要打出去
    e, res = _free_env()
    a = next(x for x in res["actors"] if x["actor_ref"] == "enemy:0")
    ref = a["attack_target_options"][0]["ref"]
    hits = max(1, a["base_hits_per_attack"])
    target = e.state.enemies[1]
    hp0 = target.current_hp
    out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [
        {"actor_ref": "enemy:0", "daowen": None,
         "attack_actions": [{"hits": [_hit(ref) for _ in range(hits)]} for _ in range(2)]}]})
    assert out["success"], out.get("error")
    assert target.current_hp < hp0, "攻击×2 应当两组命中都生效"

    # 道纹×2（不同名）+ 一攻一道纹：只要引擎接受即通过（效果按各自道纹规则）
    for tag, build in (("道纹×2", "double_daowen"), ("一攻一道纹", "mixed")):
        e, res = _free_env()
        a = next(x for x in res["actors"] if x["actor_ref"] == "enemy:0")
        opts = a["daowen_options"]
        if len(opts) < 2 and tag == "道纹×2":
            continue                      # 该怪只有 1 个合法道纹时跳过
        ref = a["attack_target_options"][0]["ref"]
        hits = max(1, a["base_hits_per_attack"])

        def dao(opt):
            cand = {"name": opt["name"], "x": 1,
                    "dodge": False, "blood_shadow": False}
            if opt.get("requires_target"):
                cand["target_ref"] = ref
            return cand

        if build == "double_daowen":
            choice = {"actor_ref": "enemy:0", "daowen": dao(opts[0]), "daowen_2": dao(opts[1]),
                      "attack_actions": []}
        else:
            choice = {"actor_ref": "enemy:0", "daowen": dao(opts[0]),
                      "attack_actions": [{"hits": [_hit(ref) for _ in range(hits)]}]}
        out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [choice]})
        assert out["success"], f"{tag} 应当被接受: {out.get('error')}"

    # 同一种道纹发两次 / 0 槽：必须被拒
    e, res = _free_env()
    a = next(x for x in res["actors"] if x["actor_ref"] == "enemy:0")
    opt = a["daowen_options"][0]
    same = {"name": opt["name"], "x": 1, "dodge": False, "blood_shadow": False}
    if opt.get("requires_target"):
        same["target_ref"] = a["attack_target_options"][0]["ref"]
    bad = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [
        {"actor_ref": "enemy:0", "daowen": same, "daowen_2": dict(same), "attack_actions": []}]})
    assert not bad["success"], "同一种道纹不得一回合发两次"
    zero = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [
        {"actor_ref": "enemy:0", "daowen": None, "attack_actions": []}]})
    assert not zero["success"], "0 槽必须被拒"


def test_free_actions_round_advances_even_without_attacks():
    """只发道纹、不打人的回合也要正常走完（不得卡在玩家行动阶段）——引擎路由回归。"""
    e = arena.build_arena(_lineup(2), free_actions=True)
    for _ in range(2):                      # 两只怪，每只：prepare → 提交（道纹×2，无攻击）
        res = _prepare(e)
        actor = next(a for a in res["actors"]      # 养蛊场逐 actor 交替：挑还没出手的那只
                     if e.state.enemies[int(a["actor_ref"].split(":")[1])]
                     .actions_used_this_round < 2)
        opts = actor["daowen_options"]
        assert opts, "测试怪应当有合法道纹"
        ref_target = actor["attack_target_options"][0]["ref"]

        def dao(opt):
            cand = {"name": opt["name"], "x": 1,
                    "dodge": False, "blood_shadow": False}
            if opt.get("requires_target"):
                cand["target_ref"] = ref_target
            return cand

        choice = {"actor_ref": actor["actor_ref"], "daowen": dao(opts[0]), "attack_actions": []}
        if len(opts) >= 2:
            choice["daowen_2"] = dao(opts[1])
        out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [choice]})
        assert out["success"], out.get("error")
    assert e.state.combat_subphase == "await_round_end", \
        "全部怪物（只有道纹、没有攻击）出手后，回合应当可以推进到回终"
    assert e.execute_action("round_end", {})["success"]


def test_free_actions_off_still_requires_daowen():
    """默认关：候选里有道纹时提交 daowen=null 仍被拒（原契约零回归）。"""
    e = arena.build_arena(_lineup(2))
    e.state.monster_free_actions = False
    res = _prepare(e)
    a = next(x for x in res["actors"] if x["actor_ref"] == "enemy:0")
    ref = a["attack_target_options"][0]["ref"]
    hits = max(1, a["base_hits_per_attack"])
    out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [
        {"actor_ref": "enemy:0", "daowen": None,
         "attack_actions": [{"hits": [_hit(ref) for _ in range(hits)]}]}]})
    assert not out["success"], "自由行动关闭时，有合法道纹就必须提交一个"


def test_self_kill_rule_has_teeth():
    """失败品判据「有牙」：同为「杀了两只后自己作死」，判 0 vs 不判 0 的差别可观测。"""
    stat = {"self_kill": True, "deaths": 1, "kills": 2, "zero_damage_rounds": 0,
            "dmg_out": 150, "dmg_taken": 40, "casts": 3, "rounds_acted": 4}
    res = {"stats": {0: dict(stat)}, "survivors": [1], "lineup": ["A", "B"], "rounds": 4}
    assert arena.fitness(res, 0, dict(arena.DEFAULT_WEIGHTS)) == 0.0
    assert arena.fitness(res, 0, dict(arena.DEFAULT_WEIGHTS), cull_self_kill=False) > 0.5


def test_ffa_kill_is_rewarded():
    """击杀对手 = 加分（适应度里含击杀项，且赢家不为 0）。"""
    res = arena.run_match(_lineup(2), dict(arena.DEFAULT_WEIGHTS), seed=0, max_rounds=8,
                          setup=lambda e: setattr(e.state.enemies[1], "current_hp", 1))
    killer = [i for i, s in res["stats"].items() if s["kills"] > 0]
    assert killer, "应当出现击杀"
    assert res["stats"][killer[0]]["self_kill"] is False
    assert arena.fitness(res, killer[0], dict(arena.DEFAULT_WEIGHTS)) > 0
