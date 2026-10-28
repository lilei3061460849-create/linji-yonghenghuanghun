"""面板不写死 X（x_free）时，怪物 AI 在发动时自选 X 的契约测试。

2026-09-16 用户令：道纹 X 不再写死在面板上，改由怪物 AI 在发动时自选，
**上限只受法限或者代价限制**。面板不带数字 → x_free，X 由提交方在 [1, max_x] 内自选；
面板带数字 → 固定 X，行为与旧版一致（迁移期两种写法都必须能跑）。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity

from tests.monster_phase_support import _decline_spells


def _engine(tmp_path, seed: int = 20260822) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "w.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=seed,
    )


def _monster(mana: int = 14, free_daowen=("弱化",)):
    m = Entity("测试怪", "怪物", blood_limit=234, current_hp=234,
               attack_count=3, attack_power=mana,
               mana_limit=mana, current_mana=mana, speed_limit=3, current_speed=3)
    for name in free_daowen:
        m.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""),
            x_value=0, x_free=True)
    return m


def _setup(e: GameEngine, monster: Entity):
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=100, speed_limit=9, current_speed=9,
                    attack_count=1, attack_power=2)
    e.state.player = player
    e.state.friends = []
    e.state.employees = []
    e.state.enemies = [monster]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.pending_monster_phase = {}
    e.state.current_round = 2      # 跳过白板回合，怪物可发动道纹
    return player


def _prepare(e):
    prep = e.execute_action("prepare_monster_phase", {})
    actor = prep["result"]["actors"][0]
    return prep, actor


def _hits(actor, atk, count=None):
    n = count if count is not None else actor["base_hits_per_attack"]
    return [{"target_ref": atk["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": _decline_spells(atk)} for _ in range(n)]


def _resolve(e, prep, actor, x):
    atk = actor["attack_target_options"][0]
    opt = actor["daowen_options"][0]
    dao = {"name": opt["name"], "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {}}
    if x is not None:
        dao["x"] = x
    if opt["requires_target"]:
        dao["target_ref"] = atk["ref"]
    return e.execute_action("resolve_monster_phase", {
        "token": prep["result"]["token"],
        "choices": [{"actor_ref": actor["actor_ref"], "daowen": dao,
                     "attack_actions": [{"hits": _hits(actor, atk)}]}],
    })


# ---------------- 正常路径 ----------------

def test_x_free_daowen_accepts_submitted_x_and_scales_cost(tmp_path):
    """正常路径：面板不写 X 时，提交方自选 X，代价按 X 缩放。

    弱化是【消耗】类，代价 2X。法限 14 → 上限 X=7，付 2×7=14。
    """
    for x, expected_paid in ((1, 2), (3, 6), (7, 14)):
        e = _engine(tmp_path)
        m = _monster(mana=14)
        _setup(e, m)
        prep, actor = _prepare(e)
        opt = actor["daowen_options"][0]
        assert opt["x_free"] is True and opt["max_x"] == 7, opt
        before = m.current_mana
        r = _resolve(e, prep, actor, x)
        assert r["success"], r.get("error")
        assert before - m.current_mana == expected_paid, (
            f"X={x} 应扣 {expected_paid} 法力，实际扣 {before - m.current_mana}")


def test_fixed_x_daowen_unchanged(tmp_path):
    """错误对照：面板写死 X 的道纹走固定 X 分支，不受自选 X 影响。"""
    e = _engine(tmp_path)
    m = Entity("测试怪", "怪物", blood_limit=234, current_hp=234,
               attack_count=3, attack_power=14, mana_limit=14, current_mana=14,
               speed_limit=3, current_speed=3)
    m.dao_wen["弱化"] = DaoWenInstance(
        DaoWen(name="弱化", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=2, x_free=False)
    _setup(e, m)
    prep, actor = _prepare(e)
    opt = actor["daowen_options"][0]
    assert opt["x_free"] is False and opt["x"] == 2
    before = m.current_mana
    r = _resolve(e, prep, actor, None)      # 不提交 x 也应成功
    assert r["success"], r.get("error")
    assert before - m.current_mana == 4     # 固定 X=2 → 2×2


# ---------------- 边界 ----------------

def test_max_x_boundary_accepted_and_exceeded_rejected(tmp_path):
    """边界：X=max_x 可发动，X=max_x+1 被拒。"""
    e = _engine(tmp_path)
    m = _monster(mana=14)                    # 上限 7
    _setup(e, m)
    prep, actor = _prepare(e)
    assert _resolve(e, prep, actor, 7)["success"]

    e = _engine(tmp_path)
    m = _monster(mana=14)
    _setup(e, m)
    prep, actor = _prepare(e)
    r = _resolve(e, prep, actor, 8)
    assert r["success"] is False
    assert "超出可负担范围" in r.get("error", ""), r.get("error")


@pytest.mark.parametrize("mutation_before", [0, 49, 150])
def test_monster_mutation_daowen_has_no_lost_cap(tmp_path, mutation_before):
    """全力按异变5X计费；怪物无迷失上限，高异变也能实际发动。"""
    e = _engine(tmp_path)
    m = _monster(mana=14, free_daowen=())
    m.mutation_count = mutation_before
    m.dao_wen["全力"] = DaoWenInstance(
        DaoWen(name="全力", formula="", cost_type="异变", cost_formula="5X",
               effect_formula=""), x_value=0, x_free=True)
    _setup(e, m)
    prep, actor = _prepare(e)
    opt = next(o for o in actor["daowen_options"] if o["name"] == "全力")
    assert opt["max_x"] == e.combat._DAOWEN_X_PROBE_CAP, opt
    result = _resolve(e, prep, actor, 10)
    assert result["success"], result
    assert m.mutation_count == mutation_before + 50
    assert m.is_alive and m.current_hp == 234


def test_unaffordable_daowen_filtered_from_prepare(tmp_path):
    """边界：连 X=1 都付不起时，prepare 不列出该道纹。"""
    e = _engine(tmp_path)
    m = _monster(mana=1)                     # 弱化 2X，X=1 就要 2 点法力 → 付不起
    _setup(e, m)
    prep, actor = _prepare(e)
    assert not [o for o in actor["daowen_options"] if o["name"] == "弱化"], \
        "付不起的道纹不应出现在 prepare 候选里"


# ---------------- 错误输入 ----------------

@pytest.mark.parametrize("bad_x", ["3", 3.0, True, [3]])
def test_non_integer_x_rejected(tmp_path, bad_x):
    """错误输入：提交的 x 必须是整数，否则拒绝。

    注意 True 是 int 的子类，必须显式排除（否则会被当成 X=1）。
    """
    e = _engine(tmp_path)
    m = _monster(mana=14)
    _setup(e, m)
    prep, actor = _prepare(e)
    r = _resolve(e, prep, actor, bad_x)
    assert r["success"] is False
    assert "x必须是整数" in r.get("error", ""), r.get("error")


def test_missing_x_falls_back_to_max(tmp_path):
    """不提交 x 时回退到可负担上限，而不是报错。

    这是"改面板不能改坏"的兜底：sim/ 下几十处怪物阶段驱动各自拼装提交字典，
    面板去掉 X 后它们不会凭空多出 x 字段。若硬报错，迁面板就等于让模拟器
    与手操流程当场跑不起来。回退到上限保证旧驱动继续可用。
    """
    e = _engine(tmp_path)
    m = _monster(mana=14)                    # 弱化 2X → 上限 7
    _setup(e, m)
    prep, actor = _prepare(e)
    before = m.current_mana
    r = _resolve(e, prep, actor, None)
    assert r["success"], r.get("error")
    assert before - m.current_mana == 14     # 回退到 X=7 → 2×7=14


def test_negative_and_zero_x_rejected(tmp_path):
    """错误输入：X 必须 ≥1。"""
    for bad in (0, -1):
        e = _engine(tmp_path)
        m = _monster(mana=14)
        _setup(e, m)
        prep, actor = _prepare(e)
        r = _resolve(e, prep, actor, bad)
        assert r["success"] is False
        assert "超出可负担范围" in r.get("error", ""), r.get("error")


# ---------------- 预演评分选 X（选案 C） ----------------

def _preview_scenario(tmp_path, dw, *, monster_hp=300, monster_mana=40,
                      monster_speed=2, player_mana=10, player_speed=3,
                      seed=20260822):
    """构造一个可预演的怪物阶段，返回 (engine, monster, actor, prep)。

    2026-10-25：怪物攻击并入玩家同一套机制后，击数＝[攻击次数]＝当前速度，
    故 attack_count 必须与 speed_limit 同值（旧面板两者可以各写一个数）。
    """
    e = _engine(tmp_path, seed)
    m = Entity("测试怪", "怪物", blood_limit=monster_hp, current_hp=monster_hp,
               attack_count=monster_speed, attack_power=monster_mana,
               mana_limit=monster_mana, current_mana=monster_mana,
               speed_limit=monster_speed, current_speed=monster_speed)
    m.dao_wen[dw] = DaoWenInstance(
        DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0, x_free=True)
    p = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
               mana_limit=player_mana, current_mana=player_mana,
               speed_limit=player_speed, current_speed=player_speed,
               attack_count=1, attack_power=player_mana)
    e.state.player = p
    e.state.friends = []
    e.state.employees = []
    e.state.enemies = [m]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.pending_monster_phase = {}
    e.state.current_round = 2
    prep = e.execute_action("prepare_monster_phase", {})
    return e, m, prep["result"]["actors"][0], prep


def _chosen_x(e, m, actor, prep, dw):
    """走 sim 侧完整的提交拼装（含正确的自身/敌方选目标）后取评分选出的 X。"""
    from sim.monster_targets import pick_monster_daowen_target, pick_monster_daowen_x
    opt = next(o for o in actor["daowen_options"] if o["name"] == dw)
    atk = actor["attack_target_options"][0]
    hits = [{"target_ref": atk["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": _decline_spells(atk)}
            for _ in range(actor["base_hits_per_attack"])]
    dao = {"name": dw, "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {}}
    # 必须用怪物 AI 的选目标：把自身回血打到玩家身上等于喂养敌方，
    # 评分会惩罚，X 恒为 1（曾据此误判"持续类被低估"）。
    if opt["requires_target"]:
        dao["target_ref"] = pick_monster_daowen_target(e, actor["actor_ref"], opt)
    choice = {"actor_ref": actor["actor_ref"], "daowen": dao,
              "attack_actions": [{"hits": hits}
                                 for _ in range(actor["base_attack_actions"])]}
    return pick_monster_daowen_x(e, m, opt, choice, prep["result"]["token"])


def test_heal_x_scales_with_missing_hp(tmp_path):
    """正常路径：【再生】按需回血，不浪费法力过量治疗。

    目标必须选自身——打到玩家身上等于喂养敌方，评分会惩罚。
    """
    # 满血：不治疗，取最小值
    e, m, actor, prep = _preview_scenario(tmp_path, "再生")
    assert _chosen_x(e, m, actor, prep, "再生") == 1
    # 缺 60 生命（再生回 4X）→ X=15 正好补满
    e, m, actor, prep = _preview_scenario(tmp_path, "再生", monster_hp=300)
    m.current_hp = 240
    assert _chosen_x(e, m, actor, prep, "再生") == 15


def test_persistent_duration_buff_scales_with_expected_rounds(tmp_path):
    """边界：持续X类道纹（此处用【全力】代替已删的【狂暴】【减速】）duration=X，
    效果下回合才兑现——单步预演看不到收益。

    补上跨回合期望收益后，X 应随"预期还能打几回合"缩放：
    预期够长时取满上限，朝不保夕时买那么久没有意义。
    """
    # 敌方每回合仅 6 伤 → 预期能打很久，值得买更长持续时间
    e, m, actor, prep = _preview_scenario(tmp_path, "全力", player_mana=2)
    long_x = _chosen_x(e, m, actor, prep, "全力")
    assert long_x > 1, f"预期剩余回合很长时不应只买 X=1，实际 {long_x}"

    # 敌方每回合 90 伤 → 怪物朝不保夕，买满时长是浪费
    e, m, actor, prep = _preview_scenario(tmp_path, "全力", player_mana=30)
    short_x = _chosen_x(e, m, actor, prep, "全力")
    assert short_x <= long_x, f"濒死时不应买比长局更久的时长：{short_x} > {long_x}"
