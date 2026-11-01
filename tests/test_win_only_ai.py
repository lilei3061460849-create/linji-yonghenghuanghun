"""胜负唯一计分 AI（WinOnlyAI）守卫测试。

用户裁定 2026-09-10：胜 +1 / 败 −1，无论什么手段什么战术。
WinOnlyAI 用「整局推演的结局 ±1」做最终裁决（启发式只裁剪提案）。
本文件守住两件事：
1. 推演世界切换不泄漏——同 seed 复跑两次逐字节一致（世界换回不干净会立刻发散）；
2. 结构性结论在 ±1 口径下复现——正席 26 血方胜、换席 4 血方负。
"""
import hashlib
import os

import sim.duel_chronicle as dc
from sim.win_only_ai import WinOnlyAI, win_only_cls

W = dc.WINNER_DIR
CH_FULL = os.path.join(W, "普攻武斗_69.json")    # 26血/速12/法12
DF_FULL = os.path.join(W, "普攻武斗_429.json")   # 4血/速10/法16


def _render_md5(rec: dict) -> str:
    return hashlib.md5(dc.render(rec).encode()).hexdigest()


def test_win_only_world_swap_no_leak_same_seed_identical():
    """同 seed 复跑两次：判定+逐手实录逐字节一致（推演世界换回不净即发散）。"""
    r1 = dc.chronicle(CH_FULL, DF_FULL, 1, ai_cls=WinOnlyAI)
    r2 = dc.chronicle(CH_FULL, DF_FULL, 1, ai_cls=WinOnlyAI)
    assert r1["verdict"] == r2["verdict"]
    assert _render_md5(r1) == _render_md5(r2)


def test_win_only_strutsural_verdicts_both_seats():
    """±1 口径下结构性结论复现：26 血方正席胜，换席 4 血方负。"""
    v = dc.chronicle(CH_FULL, DF_FULL, 1, ai_cls=WinOnlyAI)["verdict"]
    assert v["winner"] == "challenger"
    rec2 = dc.chronicle(DF_FULL, CH_FULL, 1, ai_cls=WinOnlyAI)
    v2 = rec2["verdict"]
    if v2["winner"] != "defender":
        # 2026-10-14：R1 全量曾单次翻转（后 17 次复现逐字节一致，根因未定；
        # 已排除超时伪装/哈希种子/文件内污染/透支搏命代码路径）。
        # 失败时落盘实录，供下次直接 diff 首个分叉。
        with open("/tmp/win_only_structural_fail.txt", "w", encoding="utf-8") as f:
            f.write(f"verdict={v2}\n" + dc.render(rec2))
    assert v2["winner"] == "defender"


def test_win_only_is_run_duel_pvp_default():
    """用户裁定 2026-09-10：胜负唯一计分是死斗默认口径（不许悄悄改回去）。"""
    from sim.duel_pvp import _default_ai_cls
    assert _default_ai_cls() is WinOnlyAI


def test_win_only_is_pve_default_everywhere():
    """用户裁定二审：一切打分都用胜负唯一计分——PvE 默认也是 WinOnlyAI。"""
    import inspect
    from sim import build_learner as bl
    src = inspect.getsource(bl._play)
    assert "WinOnlyAI" in src          # 默认类落点在 _play
    from sim.duel_pvp import _default_ai_cls
    assert _default_ai_cls() is WinOnlyAI
    # 混合类：遗言桥为提案器、±1 为裁决（MRO 保证裁决覆写提案器口径）
    from sim.legacy_mentor import LegacyAwareAI
    hybrid = win_only_cls(LegacyAwareAI)
    assert issubclass(hybrid, WinOnlyAI) and issubclass(hybrid, LegacyAwareAI)


def test_win_only_pve_playout_signs():
    """PvE 推演判分：残局收割=+1（世界换回不净会污染第二段）。"""
    import shutil
    import tempfile
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen
    from sim.optional_actions import start_battle, start_round

    tmp_seal = tempfile.mktemp(suffix=".json")
    shutil.copy(os.path.join(W, "普攻武斗_69.json"), tmp_seal)
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=7,
                   sealed_candidate_path=tmp_seal, death_book_path=dc.CANON_BOOK)
    e.execute_action("setup_attributes", {"name": "探针", "blood_points": 3,
                                          "speed_points": 10, "mana_points": 12})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    start_battle(e)
    start_round(e)
    ai = WinOnlyAI(e)
    cand = ai._basic_attack_candidates()[0]
    for m in e.state.enemies:
        m.current_hp = 1
    assert ai._playout_score(cand) == 1      # 收割残局 → 胜 +1
    # 「满血墙 → 推不出胜负 ≠+1」已失效（2026-09-10 实测）：攻次×攻力改制后
    # 纯普攻 10 回合可磨死 258 血墙，墙打不动人时第 5 回合死于凡庸钟——满血墙
    # +1 是合法终局，旧断言物理上不可能成立。伪造 +1 的结构保障在 _playout_pve
    # 只因「深拷贝世界内敌方全灭」返回 1；世界泄漏由本文件第一条逐字节守卫钉死。
    # 此处改钉：推演后真实世界毫发无损（预演零副作用）。
    for m in e.state.enemies:
        m.current_hp = m.blood_limit
    ai._playout_score(cand)
    for m in e.state.enemies:
        assert m.is_alive and m.current_hp == m.blood_limit


class _VetoProbe(WinOnlyAI):
    """最小桩：只喂一个候选，其余协作方全部替身（只测提案层否决与裁决序）。"""

    def __init__(self, mutation, labels_scores):
        self._actor = None
        player = type("P", (), {
            "mutation_count": mutation, "name": "探针", "is_alive": True,
            "current_mana": 0, "current_hp": 1, "shield": 0,
        })()
        state = type("S", (), {"player": player, "enemies": []})()
        self.engine = type("E", (), {"state": state, "execute_action": staticmethod(
            lambda a, p: {"success": True, "action": a})})()
        self._pv = type("V", (), {
            "preview": staticmethod(lambda a, p: {
                "result": {"success": True},
                "diff": {"player": {"mutation_delta": p.get("x", 0) * 8}},
                "all_gone": False}),
            "preview_sequence": staticmethod(lambda steps: {
                "result": {"success": True},
                "diff": {"player": {"mutation_delta": 0}},
                "all_gone": False})})()
        self.used = {}
        self._labels_scores = labels_scores          # {label: 启发式分}

    @property
    def previewer(self):
        return self._pv

    def _daowen_candidates(self):
        return [{"label": lb, "action": f"cast_{lb}", "params": {"x": 1}}
                for lb in self._labels_scores]

    def _basic_attack_candidates(self):
        return []

    def _resonance_candidates(self):
        return []

    def _ladder_candidates(self):
        return []

    def _score_candidate(self, diff, label, kind=None, target=None):
        return self._labels_scores[label]

    def _playout_score(self, cand):
        return -1                                    # 推演全败：考验平局裁决序

    def _run_steps(self, steps):
        return {"success": True}


def test_collapse_line_veto_rejects_mutation_suicide():
    """迷失阈值否决（六审训练）：异变 48 时 封印X=1（+8→56≥50）非终结 → 整个提案被拒。

    seed5 实锤：异变 48 放封印 X=1 → 异变 56 ≥ 迷失阈值 50 → 血 0 自爆。
    纯 AI 层知识，不许碰引擎规则面；终结（all_gone）豁免不在本桩覆盖内。
    """
    ai = _VetoProbe(mutation=48, labels_scores={"封印X=1": 5.0})
    assert ai._dynamic_action() is None              # 唯一候选 projected=56 → 否决


def test_collapse_line_veto_allows_safe_cast():
    """异变 30 时同一封印（30+8=38 < 50）→ 照常出手。"""
    ai = _VetoProbe(mutation=30, labels_scores={"封印X=1": 5.0})
    r = ai._dynamic_action()
    assert r is not None and r.get("success")


def test_survival_tiebreak_prefers_later_death():
    """存活时长裁决（六审训练）：推演全败时，死得晚的候选胜出。"""
    ai = _VetoProbe(mutation=10, labels_scores={"甲": 5.0, "乙": 5.0})

    def fake_score(cand):
        ai._last_survival = 5 if cand["label"] == "乙" else 2
        return -1
    ai._playout_score = fake_score
    r = ai._dynamic_action()
    assert r is not None
    # 推演全败（同 -1）→ 死得晚的「乙」胜出（非提案序首选）
    assert r["action"] == "cast_乙", f"存活裁决失效: {r}"


def test_candidate_merge_tolerates_list_params():
    """提案去重键要能吃下列表参数（2026-10-31 死斗崩溃回归）。

    【波及】的参数是 `dodge_targets=[{...}, {...}]`，【冰冻】是
    `freeze_daowen=[...]`，【流动/遗忘】是 `status_indices={ref: [...]}`。
    旧实现用 `tuple(sorted(params.items()))` 拼键，列表一进元组就
    `TypeError: unhashable type: 'list'`，整场死斗直接崩在 WinOnlyAI 里。
    """
    from sim.win_only_ai import _candidate_key, _merge_ladder

    bobo = {"action": "cast_daowen", "label": "波及X=1",
            "params": {"daowen_name": "波及", "x": 1,
                       "dodge_targets": [{"target_ref": "enemy:0", "dodge": False},
                                         {"target_ref": "enemy:1", "dodge": False}]}}
    ice = {"action": "cast_daowen", "label": "冰冻X=2",
           "params": {"daowen_name": "冰冻", "x": 2,
                      "freeze_daowen": ["杀伐", "再生"]}}
    # 两个带列表的候选必须各有各的键
    assert len({_candidate_key(bobo), _candidate_key(ice)}) == 2
    # 列表与等价元组归一到同一个键（参数容器不同不该被当成两个动作）
    assert (_candidate_key({"action": "a", "params": {"t": [1, 2]}})
            == _candidate_key({"action": "a", "params": {"t": (1, 2)}}))
    # 爬梯席并提案席：重复的不进，新的进，全程不抛 TypeError
    merged = _merge_ladder([bobo], [dict(ice), dict(bobo)])
    assert [c["label"] for c in merged] == ["波及X=1", "冰冻X=2"]
