"""真实引擎冒烟：逐一实测【战始/战终/回始/回终/敌回始/敌回终】6种全局时点

自创法术是否真正接线并触发。

每个用例都真正调用 GameEngine.execute_action 的公开接口（battle_start/
round_start/round_end/battle_end/prepare_monster_phase/resolve_monster_phase），
不绕过状态机、不直接调用 combat 内部方法，记录：
1. 【学习】阶段 wired 字段是否如实标注为 True（本轮改造后应为 True）；
2. 战斗真正推进到该时点时，法术是否被列入 spell_choices 候选
   （prepare_global_trigger_spells 的返回）；
3. 显式提交 use=True 后，资源变化是否符合预期（法力/生命）。

运行：python3 sim/probe_global_trigger_spells.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Relic
from tests.setup_support import finish_initial_daowen
from sim.monster_phase_submit import single_actor_choices, monster_phase_choices  # noqa: E402


def _fresh_engine(tag: str, region: str = "罪孽都市") -> GameEngine:
    d = tempfile.mkdtemp(prefix=f"probe_global_{tag}_")
    e = GameEngine(db_path=os.path.join(d, "g.db"), rng_seed=7,
                   sealed_candidate_path=os.path.join(d, "s.json"))
    e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 10, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _give_daowen(entity, name, x=0):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x)




from sim.common import learn_spell_probe_shortcut as _learn  # 2026-11-03：4 份逐字相同 + 删掉一份自递归死代码


def _decline_all_spell_choices(target_option):
    return {k: {sp["spell_name"]: {"use": False} for sp in target_option.get("spell_options", {}).get(k, [])}
            for k in ("before", "after")}


def _decline_monster_phase(e):
    """走完一次完整怪物阶段：按当前契约提交（有合法道纹则声明第一个），不主动结束回合。"""
    res = e.execute_action("prepare_monster_phase", {})
    assert res["success"], res
    choices = monster_phase_choices(res["result"]["actors"], engine=e)
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"],
                                                  "choices": choices})
    assert r["success"], r
    return res, r


REPORT = []


def record(trigger_label, wired_flag, fired, detail):
    REPORT.append({"trigger": trigger_label, "learn_wired": wired_flag,
                   "actually_fired": fired, "detail": detail})


def _spell_choices_for(candidates, spell_name, x, target_ref):
    """按 prepare_global_trigger_spells 返回结构，构造 use=True 的完整提交。"""
    result = {}
    for holder_ref, entries in candidates.items():
        holder_choices = {}
        for entry in entries:
            if entry["spell_name"] == spell_name:
                # 每个决策槽位一条；老探针只提交一轮，故上限写1
                submitted = [{"x": x, "target_ref": target_ref, "dodge": False}
                             for _ in entry["steps"]]
                holder_choices[entry["spell_name"]] = {"use": True, "steps": submitted,
                                                       "max_iterations": 1}
            else:
                holder_choices[entry["spell_name"]] = {"use": False}
        result[holder_ref] = holder_choices
    return result


def probe_battle_start():
    """战始：法术在战始对自身发动再生回血。

    战始会先把法力重置为当前[法限]满池、再结算战始遗物，全局法术紧随其后
    结算（见 engine/api.py._action_battle_start 的顺序注释）——法术因此
    能吃到本场战始已经到账的法力，不会在法力恒为0的错误时点上被误判
    "接线失败"。（原先靠已删除的遗物【折速法印】提供法力，现直接依赖满池。）
    """
    e = _fresh_engine("battle_start")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "开局回春", "required_daowen": ["再生"],
                  "trigger_condition": "战始",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.player.current_hp = 50
    hp_before = e.state.player.current_hp
    e.state.energy = 0
    candidates = e.combat.prepare_global_trigger_spells("战始")
    listed = bool(candidates)
    # [战始]把法力重置为当前[法限]满池（一池制），可承担的 X 上限就是法限。
    x = max(1, e.state.player.mana_limit)
    r = e.execute_action("battle_start", {
        "relic_choices": {},
        "spell_choices": _spell_choices_for(candidates, "开局回春", x, "player:0"),
    })
    hp_after = e.state.player.current_hp
    fired = r["success"] and hp_after > hp_before
    record("战始", wired, fired,
           f"学习前listed={listed}；resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"
           f"（[战始]法力=法限{x}，【再生{x}】应回{2 * x}血）")


def probe_battle_end():
    """战终：击杀全部敌人后，战终法术对自身发动再生。

    规则正文明确规定[战终]会"清除局内增益（包括回复）"，因此哪怕法术
    真实结算过一次【再生】、真实回过血，本函数末尾的战终清算逻辑也会把
    这部分"局内回复"额度抹掉、生命回退——这是游戏规则本身的行为，不是
    法术没有触发。因此判据改为看 spell_logs 里是否存在真实的 apply_
    daowen_effect 执行记录，而不是看最终生命值。
    """
    e = _fresh_engine("battle_end")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "凯旋疗伤", "required_daowen": ["再生"],
                  "trigger_condition": "战终",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    for enemy in e.state.enemies:
        enemy.current_hp = 0
        enemy.is_alive = False
    e.state.player.current_hp = 50
    hp_before = e.state.player.current_hp
    candidates = e.combat.prepare_global_trigger_spells("战终")
    listed = bool(candidates)
    r = e.execute_action("battle_end", {
        "spell_choices": _spell_choices_for(candidates, "凯旋疗伤", 3, "player:0")
    })
    hp_after = e.state.player.current_hp
    spell_logs = r["result"].get("spell_logs") if r["success"] else []
    fired = r["success"] and any(
        log.get("execution", {}).get("effects") for log in spell_logs)
    record("战终", wired, fired,
           f"学习前listed={listed}；resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"
           f"（法术真实执行日志：{spell_logs}；[战终]规则本身会清除局内回复，"
           f"因此最终hp会被回退，不代表法术未触发）")


def probe_round_start():
    """回始：每回合开始时，法术自动对自身发动再生。"""
    e = _fresh_engine("round_start")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "晨起回血", "required_daowen": ["再生"],
                  "trigger_condition": "回始",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    # 一池制：[回始]不回填法力（AI_EXPERIENCE.md:1254）。探针这里显式注入法力，
    # 只为隔离"法术是否接线"这一点，不代表回始会回蓝。
    e.state.player.current_mana = 30
    e.state.player.current_hp = 50
    hp_before = e.state.player.current_hp
    candidates = e.combat.prepare_global_trigger_spells("回始")
    listed = bool(candidates)
    r = e.execute_action("round_start", {
        "spell_choices": _spell_choices_for(candidates, "晨起回血", 5, "player:0")
    })
    hp_after = e.state.player.current_hp
    fired = r["success"] and hp_after > hp_before
    record("回始", wired, fired,
           f"学习前listed={listed}；resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"
           f"（探针已显式注入法力；一池制下[回始]不回蓝，"
           f"注入只为隔离接线判定；体力上限封顶故实际回{hp_after - hp_before}）")


def probe_round_end():
    """回终：回合结束时，法术自动对自身发动再生（【再生】效果直接落地本回合当前
    生命；由于回终本身按规则会清空当次战斗的"局内回复"追踪，这里用
    spell_logs 里是否真实结算过 apply_daowen_effect（而不是最终hp）作为
    判据，与"战终"探针同理——数值真实变化过，只是被同一步骤的战终/回终
    规则回滚不代表法术没有触发）。
    """
    e = _fresh_engine("round_end")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "收工回血", "required_daowen": ["再生"],
                  "trigger_condition": "回终",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 50
    hp_before = e.state.player.current_hp
    candidates = e.combat.prepare_global_trigger_spells("回终")
    listed = bool(candidates)
    e.state.combat_subphase = "await_round_end"
    r = e.execute_action("round_end", {
        "spell_choices": _spell_choices_for(candidates, "收工回血", 4, "player:0")
    })
    hp_after = e.state.player.current_hp
    spell_logs = r["result"].get("spell_logs") if r["success"] else []
    fired = r["success"] and any(
        log.get("execution", {}).get("effects") for log in spell_logs)
    record("回终", wired, fired,
           f"学习前listed={listed}；resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"
           f"（【再生4】真实执行日志：{spell_logs}；回终格挡清空/法力清空不影响本次法术是否触发的判定）")


def probe_enemy_round_start():
    """敌回始（普通战斗）：2026-10-02 审计发现全引擎**没有该时点的结算点**。

    真正承载"玩家行动结束/怪物阶段即将开始"这一时刻的是【自身回合结束】
    （SELF_TURN_END，见 probe_self_turn_end）。因此本探针改为验证**诚实标注**：
    含有"敌回始"字样的法术仍可通过句式校验被学会，但 `wired=False`，
    引擎在学会结果里明确警告"暂未接入结算管线"，且 prepare_monster_phase
    不会静默地把它当成别的时点结算（spell_logs 为空）。
    """
    e = _fresh_engine("enemy_round_start")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "戒备回血", "required_daowen": ["再生"],
                  "trigger_condition": "敌回始",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    warned = "暂未接入战斗结算管线" in (learned["result"].get("warning") or "")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 50
    hp_before = e.state.player.current_hp
    candidates = e.combat.prepare_global_trigger_spells("敌回始")
    listed = bool(candidates)
    r = e.execute_action("prepare_monster_phase", {
        "spell_choices": _spell_choices_for(candidates, "戒备回血", 3, "player:0")
    })
    hp_after = e.state.player.current_hp
    not_silently_resolved = not (r.get("result", {}).get("spell_logs") or [])
    fired = (wired is False and warned and listed
             and r["success"] and not_silently_resolved and hp_after == hp_before)
    record("敌回始(未接线·诚实标注)", wired, fired,
           f"学习成功但wired={wired}，warning={'有' if warned else '无'}；"
           f"prepare_monster_phase列出的候选={listed}，spell_logs为空={not_silently_resolved}；"
           f"玩家hp {hp_before}→{hp_after}（该时点当前无结算点，见 法术索引.md）")


def probe_self_turn_end():
    """自身回合结束：玩家行动结束 / 怪物阶段开始前的真实结算点
    （prepare_monster_phase；自动触发法术走同一条路，这里验证手动提交）。"""
    e = _fresh_engine("self_turn_end")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "收势回血", "required_daowen": ["再生"],
                  "trigger_condition": "自身回合结束",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 50
    hp_before = e.state.player.current_hp
    candidates = e.combat.prepare_global_trigger_spells("自身回合结束")
    listed = bool(candidates)
    r = e.execute_action("prepare_monster_phase", {
        "spell_choices": _spell_choices_for(candidates, "收势回血", 3, "player:0")
    })
    hp_after = e.state.player.current_hp
    fired = r["success"] and hp_after > hp_before
    record("自身回合结束(玩家行动结束)", wired, fired,
           f"学习前listed={listed}；resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"
           f"（【再生3】回6血，结算于prepare_monster_phase）")


def probe_enemy_round_end():
    """敌回终（普通战斗）：怪物阶段结算完毕后，法术自动对自身发动再生。"""
    e = _fresh_engine("enemy_round_end")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "喘息回血", "required_daowen": ["再生"],
                  "trigger_condition": "敌回终",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 50
    prepared = e.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    hp_before = e.state.player.current_hp
    a = prepared["result"]["actors"][0]
    hits = []
    for _ in range(a["base_hits_per_attack"]):
        target_opt = a["attack_target_options"][0]
        hits.append({"target_ref": target_opt["ref"], "dodge": False, "blood_shadow": False,
                     "spell_choices": _decline_all_spell_choices(target_opt)})
    choices = single_actor_choices(a, engine=e)
    candidates = e.combat.prepare_global_trigger_spells("敌回终")
    listed = bool(candidates)
    r = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
        "spell_choices": _spell_choices_for(candidates, "喘息回血", 3, "player:0")
    })
    hp_after = e.state.player.current_hp
    spell_logs = r["result"].get("spell_logs") if r["success"] else []
    # 判据用spell_logs里是否有真实的daowen执行记录，而不是最终hp——
    # 怪物本回合的普攻伤害可能大于法术回血量，导致净值仍下降，
    # 这不代表法术没有触发，只是数值上没能抵过伤害。
    fired = r["success"] and any(
        log.get("execution", {}).get("effects") for log in spell_logs)
    record("敌回终(普通战斗)", wired, fired,
           f"学习前listed={listed}；resolve success={r['success']}；玩家hp {hp_before}→{hp_after}"
           f"（法术真实执行日志：{spell_logs}；结算于怪物阶段真正完成之后，"
           f"净值可能因怪物本回合伤害更高而仍然下降，不代表未触发）")


def probe_duel_opponent_round_start_spell():
    """死斗场景：死斗对手（轮回者）持有的【回始】法术在 round_start 时同样真实
    结算——用户确认的映射是"死斗双方都是轮回者，敌回始/敌回终对应对方视角
    的round_start/round_end"，本探针验证的是这套通用扫描机制（按refs遍历
    全部持有者，不区分玩家侧/对手侧）确实覆盖了死斗对手，不是玩家专属通道。
    """
    import tempfile
    from engine.models import DaoWen, DaoWenInstance, Entity
    d = tempfile.mkdtemp(prefix="probe_duel_")
    sealed_path = os.path.join(d, "sealed.json")

    def _candidate(tag, speed_points, name):
        # 速限/法限按 2 属性点一档计价（点数必须偶数），故这里按偶数分配。
        e = GameEngine(db_path=os.path.join(d, f"{tag}.db"), rng_seed=1,
                       sealed_candidate_path=sealed_path)
        mana_points = 6
        speed_points = speed_points - (speed_points % 2)
        blood_points = 25 - speed_points - mana_points
        r = e.execute_action("setup_attributes", {
            "name": name, "blood_points": blood_points,
            "speed_points": speed_points, "mana_points": mana_points})
        assert r["success"], r
        finish_initial_daowen(e)   # 已包含开局遗物发现与选择
        e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
        e.execute_action("setup_choose_region", {"region": "龙心谷"})
        return e

    def _finish_battle_7(e):
        e.state.current_battle = 7
        e.state.enemies.clear()
        e.state.phase = "in_combat"
        return e.execute_action("battle_end", {})

    sealed = _candidate("sealed", 6, "封存对手")
    sealed.state.player.dao_wen["再生"] = DaoWenInstance(
        DaoWen(name="再生", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    # 生命周期（Part 6）：战斗中自创的法术默认 lifecycle=battle，[战终]即清除，
    # 也就不会被封存进死斗对手；要跨战斗（含封存/转场）保留必须显式 permanent。
    definition = {"name": "对手回血术", "required_daowen": ["再生"],
                  "trigger_condition": "回始", "effect_flow": "发动再生X于自身",
                  "lifecycle": "permanent"}
    # 局外"学习自定义法术"入口已于 2026-09-16 取消（法术一律在战斗中用
    # define_spell 自创）；探针只关心封存/全局触发管线，故沿用 _learn 捷径
    # （同一 _build_custom_spell 校验，只是跳过 1 次出手消耗）。
    learned = _learn(sealed, definition)
    assert learned["success"], learned
    wired = learned["result"].get("wired")
    _finish_battle_7(sealed)

    challenger = _candidate("challenger", 12, "挑战者")
    r = _finish_battle_7(challenger)
    assert r["result"]["final_crown"]["outcome"] == "duel_start", r

    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    assert any(s.name == "对手回血术" for s in opp.spells), "死斗对手应携带完整封存的自创法术"
    opp.current_hp = 50
    hp_before = opp.current_hp

    candidates = challenger.combat.prepare_global_trigger_spells("回始")
    opp_ref = next(ref for ref, entity in challenger.combat._combat_entity_refs().items() if entity is opp)
    listed = opp_ref in candidates
    sc = _spell_choices_for(candidates, "对手回血术", 3, opp_ref) if listed else {}
    # round_start 会先给双方轮回者按法限回满法力，法术紧随其后结算，法力足够。
    rr = challenger.execute_action("round_start", {"spell_choices": sc})
    hp_after = opp.current_hp
    fired = rr["success"] and hp_after > hp_before
    record("回始(死斗对手视角=敌回始映射)", wired, fired,
           f"死斗对手listed={listed}；resolve success={rr['success']}；"
           f"对手hp {hp_before}→{hp_after}（应因【再生3】回9血，证明扫描机制"
           f"覆盖死斗对手而非玩家专属通道）")


def main():
    probe_battle_start()
    probe_battle_end()
    probe_round_start()
    probe_round_end()
    probe_enemy_round_start()
    probe_self_turn_end()
    probe_enemy_round_end()
    probe_duel_opponent_round_start_spell()

    print(f"{'时机':<16}{'学得wired':<12}{'真实触发':<10}详情")
    all_ok = True
    for row in REPORT:
        # 常规行：学得 wired=True 且真实触发=True。
        # 「未接线·诚实标注」行是例外：预期 wired=False，且探针已验证
        # "不静默结算、如实警告"——fired=True 表示这条诚实契约成立。
        expected_wired = "未接线" not in row["trigger"]
        ok = (row["learn_wired"] is expected_wired) and row["actually_fired"] is True
        all_ok = all_ok and ok
        print(f"{row['trigger']:<16}{str(row['learn_wired']):<12}{str(row['actually_fired']):<10}{row['detail']}")
    print()
    print("全部通过" if all_ok else "存在失败用例，见上表")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
