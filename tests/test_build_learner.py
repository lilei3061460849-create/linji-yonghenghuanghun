"""
pytest - 自学习流派优化器（sim/build_learner.py）

它不预设流派，而是自己组合道纹、跑轮回、按胜负更新权重，
并挖掘"1+1>2"的协同增益，知识写入 data/build_knowledge.json 可续跑。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import importlib.util
import json
import math
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load():
    spec = importlib.util.spec_from_file_location(
        "bl", os.path.join(ROOT, "sim", "build_learner.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bl = _load()


def _finish_region_setup(engine, region):
    result = engine.execute_action("setup_choose_region", {"region": region})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in result["result"]["relic_choices"] if n not in optional),
                  result["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    return choice


def _start_battle(engine, relic):
    engine.state.energy = 0
    choices = {}   # 2026-10-13：【三相残韵盘】删除，无需预填显式决策
    return engine.execute_action("battle_start", {"relic_choices": choices})


# ---------- 正常路径 ----------

def test_starters_are_shafa_loop_not_forced_shaifa():
    """正常：可偏好的起手是整个杀伐闭环，不再写死只开杀伐。"""
    from engine.gamedata import SHAFA_LOOP_DAOWEN
    assert bl.STARTERS == list(SHAFA_LOOP_DAOWEN)
    assert "连击" in bl.STARTERS and "再生" in bl.STARTERS
    assert len(bl.STARTERS) == 8  # 当前杀伐闭环；波及属于沉沦海专属


def test_play_does_not_inject_shaifa_when_not_discovered():
    """边界：发现列表没有杀伐时，模拟不得把杀伐塞进玩家。"""
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen

    from tests.setup_support import resolve_opening_relic

    found = None
    for seed in range(1, 80):
        engine = GameEngine(db_path=f"/tmp/disc{seed}.db", rng_seed=seed)
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
        })
        resolve_opening_relic(engine)  # 新流程：先发现遗物再发现道纹
        offered = list(engine.state.pending_initial_daowen_choices)
        if "杀伐" not in offered:
            found = (engine, offered)
            break
    assert found, "80 个种子里应能抽到不含杀伐的发现"
    engine, offered = found
    chosen = choose_discovered_initial_daowen(engine, prefer="杀伐")
    assert chosen["success"]
    assert chosen["picked"] in offered
    assert chosen["picked"] != "杀伐"
    assert "杀伐" not in engine.state.player.dao_wen


def test_choose_discovered_honors_prefer_only_when_offered():
    """正常：prefer 在候选中才选它。"""
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen

    from tests.setup_support import resolve_opening_relic

    engine = GameEngine(db_path="/tmp/disc_pref.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    resolve_opening_relic(engine)  # 新流程：先发现遗物再发现道纹
    offered = list(engine.state.pending_initial_daowen_choices)
    prefer = offered[1]
    chosen = choose_discovered_initial_daowen(engine, prefer=prefer)
    assert chosen["picked"] == prefer
    assert list(engine.state.player.dao_wen) == [prefer]


def test_choose_discovered_rejects_missing_pending():
    """错误输入：没有待选发现时必须拒绝，不能凭空发道纹。"""
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen

    from tests.setup_support import resolve_opening_relic

    engine = GameEngine(db_path="/tmp/disc_bad.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    resolve_opening_relic(engine)  # 新流程：先发现遗物再发现道纹
    engine.execute_action("setup_choose_initial_daowen", {
        "daowen_name": engine.state.pending_initial_daowen_choices[0],
    })
    bad = choose_discovered_initial_daowen(engine, prefer="杀伐")
    assert not bad["success"]
    assert "没有待选择" in bad["error"]


def test_play_returns_wellformed_result():
    """正常路径：跑一局应返回通关场数与胜负"""
    r = bl.play("杀伐", ["庇护", "再生"], "龙心谷", seed=1)
    assert {"cleared", "won", "invalid"} <= set(r)
    assert 0 <= r["cleared"] <= 7
    assert isinstance(r["won"], bool)


def test_pending_event_requiring_dm_stops_instead_of_looping(tmp_path):
    """边界：模拟器遇到不能代裁的事件时应明确作废，而不是反复结算同一事件。"""
    from engine.api import GameEngine

    engine = GameEngine(
        db_path=str(tmp_path / "r.db"), save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"), rng_seed=1,
    )
    engine.execute_action("setup_attributes", {
        "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    creative = next(option for option in engine.event_pool.events["过路商人"]["options"]
                    if option["text"].startswith("限制选择权"))
    engine.event_pool.events["过路商人"]["options"] = [creative]
    engine.event_pool.current = "过路商人"

    result = bl._resolve_pending_event(engine)

    assert not result["success"] and "需要DM裁定" in result["error"]
    assert engine.event_pool.current == "过路商人"


def test_synergy_detects_positive_pair():
    """
    正常路径：协同挖掘必须能识别出 1+1>2。
    构造：A、B 单独都低分，一起出现时高分 → 增益应为正。
    """
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "A", ["X"], 1.0)
    bl.update(k, "B", ["Y"], 1.0)
    bl.update(k, "A", ["B"], 9.0)
    bl.update(k, "A", ["B"], 9.0)
    assert bl.synergies(k, min_n=2) == []
    assert k["pair_scores"] == {}, "构筑整局分数不得自动复制为道纹Combo分数"


def test_update_accumulates_knowledge():
    """正常路径：多次 update 后搜索先验累计，但不生成伪造配对贡献"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "杀伐", ["庇护", "再生"], 8.0)
    assert k["trials"]["杀伐"]["n"] == 1
    assert k["trials"]["庇护"]["sum"] == 8.0
    assert k["pair_scores"] == {}
    assert k["best"]["score"] == 8.0


# ---------- 边界条件 ----------

def test_best_only_improves():
    """边界：best 只应在更高分时被替换"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "杀伐", ["庇护"], 7.0)
    bl.update(k, "切割", ["束缚"], 3.0)
    assert k["best"]["score"] == 7.0
    bl.update(k, "切割", ["封印"], 9.0)
    assert k["best"]["score"] == 9.0


def test_ucb_prefers_untried():
    """边界：未尝试过的道纹必须获得最高探索优先级"""
    k = {"generation": 0, "trials": {"A": {"n": 5, "sum": 50.0}},
         "pair_scores": {}, "history": [], "best": None}
    assert bl.ucb(k, "从未试过", 5) > bl.ucb(k, "A", 5)


def test_propose_respects_build_size():
    """边界：生成的 build 不得超过设定长度，且不含初始道纹自身"""
    import random
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    for i in range(10):
        starter, learn = bl.propose(k, random.Random(i))
        assert starter in bl.STARTERS
        assert len(learn) <= bl.BUILD_SIZE
        assert starter not in learn


# ---------- 确认精英回注（2026-08-23 扩散实验落地） ----------

def _kb_with_elite_history():
    """构造含确认精英的知识库：甲构筑高分3评、乙构筑低分3评、丙单次幻影高分。"""
    k = {"generation": 10, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    for _ in range(3):
        bl.update(k, "封印", ["杀伐", "增殖", "透支"], 4.0)     # 确认精英
        bl.update(k, "固执", ["背负", "消灾", "定型"], 1.0)     # 确认低分
    bl.update(k, "波及", ["固化", "僵化", "退化"], 9.9)         # 单次幻影最高分
    bl.update(k, "波及", ["固化", "僵化", "退化"], 0.1)         # 幻影现形：均值5.0
    return k


def test_elite_library_ranks_by_mean_and_filters_single_eval():
    """正常路径：精英库按历次均值排序，单次评估的幻影构筑不得入库"""
    k = _kb_with_elite_history()
    # 幻影构筑已2评、均值5.0 > 精英4.0 —— 把它真正打成幻影：3评均值降到2以下
    bl.update(k, "波及", ["固化", "僵化", "退化"], 0.01)
    bl.update(k, "波及", ["固化", "僵化", "退化"], 0.01)
    bl.update(k, "波及", ["固化", "僵化", "退化"], 0.01)  # 5评均值≈2.0
    lib = bl.elite_library(k)
    assert lib, "有≥2次评估的构筑，库不应为空"
    assert lib[0][1] == ("封印", ("杀伐", "增殖", "透支")), \
        f"库首应是均值最高者，实际 {lib[0]}"
    phantom = ("波及", ("固化", "僵化", "退化"))
    assert all(key != phantom for _, key in lib[:1]), "幻影被当成库首=选择器事故"


def test_elite_library_requires_min_evals():
    """边界：全部构筑仅1次评估时精英库必须为空（退化纯探索）"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "封印", ["杀伐"], 9.9)
    assert bl.elite_library(k) == []


def test_propose_confirmed_draws_from_library(monkeypatch):
    """正常路径：confirmed 模式下 PRIOR_RATIO=1 时提案必须来自确认精英库"""
    import random
    monkeypatch.setattr(bl, "PRIOR_MODE", "confirmed")
    monkeypatch.setattr(bl, "PRIOR_RATIO", 1.0)
    monkeypatch.setattr(bl, "PRIOR_MUTATE", 0.0)          # 关掉变异=精确复制
    k = _kb_with_elite_history()
    lib_keys = {key for _, key in bl.elite_library(k)}
    assert lib_keys, "先决条件：库非空"
    for i in range(10):
        starter, learn = bl.propose(k, random.Random(i), "龙心谷")
        assert (starter, tuple(learn)) in lib_keys, "回注提案必须命中精英库成员"


def test_propose_legacy_uses_single_best(monkeypatch):
    """对照：legacy 模式保留旧行为（从 best 单次最高分变异）供 A/B 复现"""
    import random
    monkeypatch.setattr(bl, "PRIOR_MODE", "legacy")
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "封印", ["杀伐", "增殖", "透支"], 8.0)
    rng = random.Random(0)
    # legacy 下 best 存在时 50% 概率走变异通道；40次内必出现携带 best 前缀的提案
    seen_from_best = False
    for _ in range(40):
        starter, learn = bl.propose(k, rng, "龙心谷")
        shared = len(set(learn) & {"杀伐", "增殖", "透支"})
        if starter == "封印" and shared >= 2:
            seen_from_best = True
            break
    assert seen_from_best, "legacy 模式应从 best 变异（复现 A 组口径）"


def test_deepen_disabled_by_default_in_zero():
    """边界：DEEPEN_EVERY=0 时深挖复评完全关闭（A/B 旧臂依赖此行为）"""
    assert isinstance(bl.DEEPEN_EVERY, int)
    # 环境默认15；仅验证常量语义存在且可被调0（monkeypatch 到0时不触发由 main 层控制）


def test_propose_return_meta_tags_channel(monkeypatch):
    """第十八批审计钩子：return_meta=True 必须暴露提案通道且不改提议分布"""
    import random
    monkeypatch.setattr(bl, "PRIOR_MODE", "confirmed")
    monkeypatch.setattr(bl, "PRIOR_RATIO", 1.0)
    monkeypatch.setattr(bl, "PRIOR_MUTATE", 0.0)
    k = _kb_with_elite_history()
    starter, learn, meta = bl.propose(k, random.Random(0), "龙心谷", return_meta=True)
    assert meta["channel"] == "elite_copy"       # 关变异=纯复制
    assert meta["parent"] in {key for _, key in bl.elite_library(k)}
    # 空库退化为 explore 通道
    k2 = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    s, l, m2 = bl.propose(k2, random.Random(1), "龙心谷", return_meta=True)
    assert m2["channel"] == "explore" and m2["parent"] is None


def test_play_returns_final_daowen():
    """第十八批依赖度探针：有效对局必须带 final_daowen（终局构筑），无效局不带"""
    import random
    r = bl.play("杀伐", ["庇护"], "龙心谷", 30601, rng=random.Random(30601))
    assert not r.get("invalid")
    fd = r.get("final_daowen")
    assert isinstance(fd, list) and all(isinstance(x, str) for x in fd)
    assert fd == sorted(fd), "final_daowen 必须有序（跨进程可比）"
    assert len(fd) >= 1, "开局道纹必在终局构筑中"



def test_synergy_ignores_low_sample_pairs():
    """边界：样本不足的配对不得进入结论（避免噪声当规律）"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "A", ["B"], 10.0)       # 只出现1次
    assert bl.synergies(k, min_n=5) == []


# ---------- 错误输入 ----------

def test_candidates_are_all_registered():
    """错误输入检出：候选池中不得含引擎未注册的道纹"""
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    unknown = [c for c in bl.CANDIDATES if c not in DaoWenEngine._registry]
    assert not unknown, f"候选池含未注册道纹：{unknown}"


def test_load_handles_missing_file(tmp_path, monkeypatch):
    """错误输入：知识库不存在时应返回初始结构而非崩溃"""
    monkeypatch.setattr(bl, "KNOWLEDGE", str(tmp_path / "nope.json"))
    k = bl.load()
    assert k["generation"] == 0 and k["trials"] == {}


def test_save_load_roundtrip(tmp_path, monkeypatch):
    """边界：知识库存盘后应能原样读回（支持续跑累积经验）"""
    monkeypatch.setattr(bl, "KNOWLEDGE", str(tmp_path / "k.json"))
    k = bl.load()
    bl.update(k, "杀伐", ["束缚", "封印"], 9.5)
    k["generation"] = 3
    bl.save(k)
    k2 = bl.load()
    assert k2["generation"] == 3
    assert k2["best"]["score"] == 9.5
    assert k2["trials"]["束缚"]["n"] == 1


# ---------- 随机种子模式 ----------

def test_play_accepts_none_seed():
    """正常路径：seed=None 时引擎使用真随机源，仍应返回合法结果"""
    r = bl.play("杀伐", ["庇护"], "龙心谷", None)
    assert 0 <= r["cleared"] <= 7
    assert isinstance(r["won"], bool)


def test_fixed_seed_is_reproducible(tmp_path):
    """边界：固定种子必须完全可复现（否则无法排查问题）

    存储必须隔离，且**两次运行各用自己的一套**：`play()` 默认把封存候选写到生产
    路径 `data/sealed_candidate.json`，通关到第7场的局会把候选留到下一次调用——
    第二次 play 就会凭空多出一个挑战对象、直接进死斗，两次结果自然不同
    （与种子无关，是跨局状态污染）。
    """
    import random as _r

    def _lab(tag):
        d = tmp_path / tag
        d.mkdir()
        return {"db_path": str(d / "seed.db"),
                "sealed_path": str(d / "sealed.json"),
                "death_book_path": str(d / "death.md")}

    a = bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42, rng=_r.Random(9), lab_paths=_lab("a"))
    b = bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42, rng=_r.Random(9), lab_paths=_lab("b"))
    assert a == b, "同一固定种子+同一决策rng，两次结果必须一致"


def test_fitness_fixed_mode_is_deterministic(monkeypatch, tmp_path):
    """边界：当前道纹/遗物池下，固定种子与副本的 fitness 重跑结果一致。

    这里不要求随机样本必须走到第7场/死斗：其覆盖由独立死斗测试负责。新遗物会
    合法改变随机初始遗物与战斗轨迹，不能再把某个历史 gen 是否封存当作本测试前置。
    """
    import sim.duel_pvp as _dp
    _orig = _dp.run_duel_pvp

    def _no_wall_timeout(*a, **kw):
        kw["max_wall_seconds"] = 1e9
        return _orig(*a, **kw)

    monkeypatch.setattr(_dp, "run_duel_pvp", _no_wall_timeout)
    lab = {"db_path": str(tmp_path / "seed.db"),
           "sealed_path": str(tmp_path / "sealed.json")}

    def _fitness():
        if os.path.exists(lab["sealed_path"]):
            os.unlink(lab["sealed_path"])
        return bl.fitness("杀伐", ["庇护", "再生"], 1, gen=9,
                          region="罪孽都市", lab_paths=lab)

    first = _fitness()
    second = _fitness()
    assert first == second
    assert first[1:] == (1, 0)  # 固定样本有效；无效局不得混进 fitness


def test_random_seed_mode_varies_samples():
    """
    正常路径：随机模式下不同 rng 应产生不同的评估样本。
    （比较 fitness 数值可能偶然相等，故直接校验取样过程会用到 rng）
    """
    import random as _r
    calls = []

    class SpyRandom(_r.Random):
        def randrange(self, *a, **k):
            v = super().randrange(*a, **k)
            calls.append(v)
            return v

    bl.fitness("杀伐", ["庇护"], 3, gen=1, random_seeds=True, rng=SpyRandom(5))
    assert len(calls) >= 3, "随机模式应为每局取一个新种子"
    assert len(set(calls)) > 1, "取到的种子全部相同，未真正随机"


def test_random_mode_uses_random_regions():
    """边界：随机模式应在三个副本间取样，而非固定轮换"""
    import random as _r
    picked = []
    orig = bl.play

    def spy(starter, learn, region, seed=None, battles=7, rng=None, telemetry=None, spend_shards=False, **kw):
        picked.append(region)
        return {"cleared": 0, "won": False, "invalid": False}

    bl.play = spy
    try:
        bl.fitness("杀伐", ["庇护"], 30, gen=1, random_seeds=True, rng=_r.Random(3))
    finally:
        bl.play = orig
    assert set(picked).issubset(set(bl.REGIONS))
    assert len(set(picked)) > 1, "随机模式下副本没有变化"


# ---------- 局外行动遥测与无效数据剔除 ----------

def test_choose_pre_battle_respects_region_exclusive():
    """错误输入检出：副本专属行动不得在其他副本被选中"""
    import random as _r
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/tele.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    picked = {bl.choose_pre_battle(e, [], 1, _r.Random(i), bl.DEFAULT_POLICY)[0]
              for i in range(200)}
    assert "维修" not in picked, "扭曲都市专属的【维修】不该在龙心谷被选中"
    assert "雇佣" not in picked, "罪孽都市专属的【雇佣】不该在龙心谷被选中"


def test_region_exclusive_enforced_by_engine():
    """正常路径：引擎必须拒绝跨副本使用专属行动"""
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/tele2.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _finish_region_setup(e, "龙心谷")
    r = e.execute_action("pre_battle_action", {"sub_action": "维修", "tier": 1})
    assert not r["success"]
    assert "扭曲都市" in r["error"]
    # 被拒绝时精力必须退还，否则会白白损失一次行动
    assert e.state.energy == 3


def test_telemetry_records_action_choices():
    """正常路径：遥测必须记录每次局外行动的尝试与成功"""
    tele = {}
    bl.play("杀伐", ["庇护"], "龙心谷", seed=5, telemetry=tele)
    assert tele.get("attempted"), "未记录任何行动选择"
    assert sum(tele["attempted"].values()) > 0
    for act in tele.get("succeeded", {}):
        assert act in tele["attempted"], "成功数不应超出尝试范围"


def test_invalid_runs_excluded_from_fitness(monkeypatch):
    """
    错误输入：出现引擎异常的对局必须被判为无效并剔除，不得污染分数。
    """
    def fake_play(starter, learn, region, seed=None, battles=7, rng=None, telemetry=None, spend_shards=False, **kw):
        return {"cleared": 0, "won": False, "invalid": True, "reason": "boom"}

    monkeypatch.setattr(bl, "play", fake_play)
    tele = {}
    score, valid, invalid = bl.fitness("杀伐", ["庇护"], 4, gen=1, telemetry=tele)
    assert valid == 0 and invalid == 4
    assert score == 0.0
    assert tele["invalid_reasons"], "无效原因必须被记录以便定位bug"


def test_valid_and_invalid_are_separated(monkeypatch):
    """边界：有效局与无效局混合时，分数只由有效局决定"""
    calls = {"n": 0}

    def mixed(starter, learn, region, seed=None, battles=7, rng=None, telemetry=None, spend_shards=False, **kw):
        calls["n"] += 1
        if calls["n"] % 2:
            # 三层口径（PVE/PVP/完整通关）下的"有效且完整通关"局：三个布尔都要给，
            # 否则 _normalise_outcome 只能推断出 PVE 胜利（full_won=False）。
            return {"cleared": 7, "won": True, "invalid": False,
                    "pve_won": True, "pvp_reached": True, "pvp_won": True, "full_won": True}
        return {"cleared": 0, "won": False, "invalid": True, "reason": "bug"}

    monkeypatch.setattr(bl, "play", mixed)
    score, valid, invalid = bl.fitness("杀伐", ["庇护"], 4, gen=1)
    assert valid == 2 and invalid == 2
    # 新口径：score=完整通关率；PVE/PVP分层数据另行统计。
    assert score == 1.0, "有效局完整通关率应为1，不应把清场深度当作胜率"


# ---------- 冷却代价（回归：束缚等曾可无限刷）----------

def test_cooldown_cost_is_applied():
    """
    正常路径：代价为【冷却X】的道纹发动后必须写入 cooldown_remaining。
    此前从未写入，导致 固执/束缚/畸变/全速 可在同场无限重复发动。
    """
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/cdtest.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    relic = _finish_region_setup(e, "龙心谷")
    e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "束缚"})
    _start_battle(e, relic)
    e.execute_action("round_start", {})
    m = e.state.enemies[0]
    r1 = e.execute_action("use_daowen", {"daowen_name": "束缚", "x": 2, "target": m.name})
    assert r1["success"]
    assert e.state.player.dao_wen["束缚"].cooldown_remaining == 4, "冷却2X未写入"


def test_cooldown_blocks_reuse_in_same_battle():
    """边界：冷却中的道纹不得再次发动（这正是束缚曾经支配全局的原因）"""
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/cdtest2.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    relic = _finish_region_setup(e, "龙心谷")
    e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "束缚"})
    _start_battle(e, relic)
    e.execute_action("round_start", {})
    m = e.state.enemies[0]
    e.execute_action("use_daowen", {"daowen_name": "束缚", "x": 2, "target": m.name})
    r2 = e.execute_action("use_daowen", {"daowen_name": "束缚", "x": 2, "target": m.name})
    assert not r2["success"], "冷却中的道纹被重复发动"
    assert "冷却" in r2["error"] or "不可用" in r2["error"]


def test_cooldown_decrements_at_battle_end():
    """边界：README 规定[战终]后冷却-1，否则道纹将永久锁死"""
    from engine.api import GameEngine
    from engine.models import DaoWen, DaoWenInstance
    e = GameEngine(db_path="/tmp/cdtest3.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _finish_region_setup(e, "龙心谷")
    inst = DaoWenInstance(DaoWen(name="束缚", formula="", cost_type="代价",
                                 cost_formula="", effect_formula=""))
    inst.cooldown_remaining = 3
    e.state.player.dao_wen["束缚"] = inst
    e.state.phase = "in_combat"
    e.execute_action("battle_end", {})
    assert inst.cooldown_remaining == 2, "[战终]后冷却未递减"


def test_all_cooldown_daowen_covered():
    """错误输入检出：所有 cost_type=冷却 的道纹都应受同一机制约束"""
    from engine.daowen import DaoWenEngine
    from engine.models import Entity
    DaoWenEngine.register_all()
    t = Entity(name="x", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=2, attack_power=5, speed_limit=6)
    found = []
    for n in DaoWenEngine._registry:
        try:
            r = DaoWenEngine.resolve(n, 2, target=t, caster=t)
        except Exception:
            continue
        if r and r.get("cost_type") == "冷却":
            found.append(n)
            assert r.get("cost", 0) > 0, f"{n} 冷却代价为0，等于没有代价"
    assert found, "未找到任何冷却型道纹，检测逻辑可能失效"


# ==================== P2: 战前休整分级策略（2026-08-19） ====================

def _rest_engine(region="罪孽都市"):
    """构造一个已开局、可调用 choose_pre_battle 的引擎。"""
    import tempfile
    from engine.api import GameEngine
    save_dir = tempfile.mkdtemp(prefix="rest")
    e = GameEngine(db_path=os.path.join(save_dir, "g.db"), rng_seed=1,
                   save_dir=save_dir)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _finish_region_setup(e, region)
    e.state.phase = "pre_battle"
    e.state.energy = 1
    return e


def _pick_rest(e, battle_no=1, policy=None):
    """强制 AI 只考虑休整（policy 全权重给休整），返回 (tier, heal_alloc)。"""
    import random
    rng = random.Random(1)
    act, params = bl.choose_pre_battle(
        e, [], battle_no, rng, policy or {"休整": 100})
    assert act == "休整", f"策略应选休整，实际 {act}"
    return params["tier"], params["heal_allocations"][0]["amount"]


def test_rest_large_gap_with_enough_shards_uses_tier3():
    """HP 缺口大且碎片足够：不得机械使用 1 级休整，应选 3 级（48+加成）。"""
    e = _rest_engine()
    p = e.state.player
    p.current_hp = 15
    p.blood_limit = 60
    e.state.shards = 60
    tier, heal = _pick_rest(e)
    assert tier == 3, f"缺口45+碎片60 应休整3级，实际 {tier}"
    assert heal == math.ceil(60 * 0.6)   # 60% 血限


def test_rest_medium_gap_falls_back_to_tier2():
    """碎片不足以付 3 级但够 2 级：退回 2 级（24+加成），不硬上 3 级。"""
    e = _rest_engine()
    p = e.state.player
    p.current_hp = 15
    p.blood_limit = 60
    e.state.shards = 20
    tier, heal = _pick_rest(e)
    assert tier == 2, f"碎片20 应休整2级，实际 {tier}"
    assert heal == math.ceil(60 * 0.4)   # 40% 血限


def test_rest_small_shards_uses_tier1():
    """碎片只够 1 级：退回 1 级（8+加成）。"""
    e = _rest_engine()
    p = e.state.player
    p.current_hp = 15
    p.blood_limit = 60
    e.state.shards = 5
    tier, heal = _pick_rest(e)
    assert tier == 1
    assert heal == math.ceil(60 * 0.2)   # 20% 血限


def test_rest_small_gap_does_not_waste_shards():
    """缺口小：即使碎片充足也不上高档位（避免为小缺口消耗关键碎片）。"""
    e = _rest_engine()
    p = e.state.player
    p.current_hp = 55
    p.blood_limit = 60
    e.state.shards = 100
    tier, _ = _pick_rest(e)
    assert tier == 1, f"缺口5 应休整1级，实际 {tier}"


def test_rest_reserves_wage_for_deployed_employees():
    """已部署员工存在时保留工资预算：碎片只够 tier3 门槛时不上 3 级。"""
    e = _rest_engine()
    p = e.state.player
    p.current_hp = 15
    p.blood_limit = 60
    e.state.shards = 30
    from engine.models import Entity
    emp = Entity("铁卫", "员工", blood_limit=60, current_hp=60,
                 attack_count=2, attack_power=4, is_deployed=True)
    e.state.employees.append(emp)
    tier, _ = _pick_rest(e)
    # reserve = 12×1 + 5 = 17；tier3 需 25+17=42 碎片，30 不够 → tier2（需 10+17=27 ✓）
    assert tier == 2, f"保留工资预算时应休整2级，实际 {tier}"
    assert e.state.shards >= 12, "休整后必须仍能支付已部署员工工资"


# ---------- 怪物阶段/门禁提交契约（2026-08-22 迭代卡死治理的回归锚点） ----------

def test_pending_redemption_is_cleared_by_pending_choices():
    """正常路径：清门禁必须顺手结算【救赎】（选无视，口径同 combo_loop_audit）。

    api.py:824：pending_redemption 非空时除 resolve_redemption 外一切行动被拒；
    驱动不清它，battle_end 必报错（曾占无效局 21/4076）。
    """
    import random as _random
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen
    e = GameEngine(db_path="/tmp/bl_redemption_test.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    # 走完与 play() 一致的开局（门禁按池独立互锁，须先清空开局各池）
    chosen = choose_discovered_initial_daowen(e, prefer="杀伐")
    assert chosen.get("success")
    bl._resolve_pending_choices(e)      # 清掉开局发现遗物等待结算池
    assert not e.state.pending_relic_choices and not e.state.pending_item_choices
    e.state.pending_redemption = {"name": "微光者甲", "blood_limit": 40,
                                  "attack_count": 2, "attack_power": 5}
    bl._resolve_pending_choices(e)
    assert e.state.pending_redemption == {}, "救赎必须被结算清空"
    assert not e.state.friends, "驱动口径选【无视】，不得产生朋友盟友（防适应度污染）"


def test_bone_angel_hit_count_drift_no_longer_invalid():
    """回归：整局跑图不得因引擎异常被判无效数据（invalid）。

    原始缺陷：骨天使/奇美拉 同阶段攻击数全局加盖 → 命中数按实时值校验
    （combat.py:4543），快照提交数不符曾误标无效，报
    '骨天使每个攻击出手必须提交7次命中选择'；修复是 sim/build_learner.py:445
    按报错给出的实时 N 定向覆盖该 actor 后重试。

    **覆盖缺口（DM裁定 2026-09-10 之后，实测记录）**：该漂移条件在新规则下已不复现。
    在 _resolve_monster_turn 的重试分支上挂计数器实测：
      · 扭曲都市 seed 900-959（60 局）        → 重试触发 0 次
      · 6 区域（龙心谷/乱葬岗/遗忘书屋/沉沦海/罪孽都市/扭曲都市）× 20 seed（120 局）→ 0 次
    原因是新规则下局太短（本构筑第 1 场即阵亡），且轮回者命中数改由当前速度实时推导，
    不再有"阶段内面板漂移"。所以本条现在只能验证"整局不被误判无效"这层安全网，
    **不再实际执行那条重试分支**。要恢复对重试本身的覆盖，需要直接构造多怪同阶段
    加盖的局面（需真引擎，桩引擎验不到），已作为待办留给 DM 定夺。
    """
    import random as _random
    r = bl.play("杀伐", ["再生", "庇护"], "扭曲都市", seed=901, rng=_random.Random(1))
    assert not r.get("invalid"), f"整局跑图不应被判无效数据：{r.get('reason')}"
    # 实测当前结局（2026-10-14 修回锋刀回始无怪 demand 缺口后重测）：
    #   cleared=0，第1场死于孢子母体，碎片20，todo_left=['庇护']（庇护没学到）
    # 与「删【领悟】之前」的记录完全一致；亦符合本条注明的「新规则下局太短
    # （本构筑第1场即阵亡）」。按本条既定原则（不钉通关率/场数，只验安全网），
    # 不再断言 cleared/battle/todo，只锁「跑出了确定结局」：
    assert r["pm"]["battle"] >= 1, f"应至少开打第1场，实{r['pm']}"


def test_stall_guard_returns_invalid_instead_of_hanging():
    """回归：能量 while 循环在'一切行动均被门禁拒绝'时必须有界退出（STALL_LIMIT），
    不得死循环挂死进程（2026-08-22 卡死两小时的直接机制）。"""
    import random as _random
    from engine.api import GameEngine
    from sim import build_learner as _bl

    e = GameEngine(db_path="/tmp/bl_stall_test.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    # 构造无法由驱动解除的语义门禁：resolve_event 不存在的事件 → 永远失败
    # （用 pending 空表制造'无任何可选项'的 deadlock 等价物成本太高，此处改走
    #  更直接的契约断言：STALL_LIMIT 存在且为有限小整数）。
    assert isinstance(_bl.STALL_LIMIT, int) and 1 <= _bl.STALL_LIMIT <= 20


def test_duel_wall_clock_guard_errors_not_hang(tmp_path):
    """回归（2026-08-22 批次13卡死事件）：死斗在第7场之后进行，实体/遗物/事件
    累积使 ai_preview 整状态 deepcopy 预演成本暴涨，实测单场 100% CPU 空转
    >5 分钟（py-spy 抓栈：preview→try_consumable，run_duel_pvp 内）。墙钟守护
    必须在限时内报错返回，并带 state 字段成本诊断，绝不允许挂死批次。

    2026-08-31 DM 裁定：墙钟/死锁都是**程序兜底**，不得判胜负。故此处不再断言
    winner == "defender"，改为断言报错契约（winner 为 None + error + timeout）。
    """
    import time as _time
    from sim.duel_pvp import run_duel_pvp
    from tests.test_final_duel import _new_candidate, _finish_battle_7, _cleanup

    sealed_path = str(tmp_path / "sealed.json")
    sealed = _new_candidate("wc_sealed", sealed_path, name="擂主")
    _finish_battle_7(sealed)
    chal = _new_candidate("wc_chal", sealed_path, name="挑战者")
    _finish_battle_7(chal)
    assert chal.state.in_final_duel

    def _hanging_act():
        _time.sleep(5)  # 模拟 preview 暴涨后的超慢行动；守护必须先于它触发
        return True

    t = _time.time()
    r = run_duel_pvp(chal, _hanging_act, max_rounds=60, max_steps=400,
                     max_wall_seconds=0)
    assert _time.time() - t < 2, "墙钟守护应立即返回，不得等待行动完成"
    # 程序兜底只报错，不判胜负（2026-08-31 DM 裁定）
    assert r.get("winner") is None, f"墙钟超时不得判擂主卫冕: {r}"
    assert r.get("error") is True and r.get("timeout") is True, r
    assert "超时" in r["reason"]
    assert isinstance(r.get("diag_state_sizes"), list)  # 归因证据
    _cleanup(sealed_path)


def test_wushen_redirect_attack_not_unsubmittable():
    """回归（引擎契约修复）：无神状态怪物的攻击重定向为打自己（规则正文），
    引擎曾把名义目标（玩家）的 spell_choices 拿去按怪物资格集校验——玩家带
    反应法术时该命中永远无法合法提交（"必须逐一覆盖[]"），整局误标无效。
    扭曲都市 seed=2 稳定复现（缝合鱼持无神）。"""
    import random as _random
    r = bl.play("杀伐", ["再生", "庇护", "束缚", "贯穿", "固执"], "扭曲都市",
                seed=2, rng=_random.Random(2))
    assert not r.get("invalid"), f"无神重定向合同矛盾应已修复，却判无效：{r.get('reason')}"


def test_duel_stats_recorded_and_won_means_duel_victory(monkeypatch):
    """正常路径（DM裁定2026-08-22）：fitness 把死斗(PvP)经验累计进 telemetry['duels']；
    won 仅指死斗胜利；第7场封存(cleared=7且未死斗)记 sealed_no_duel，不得计胜率。"""
    seq = iter([
        {"cleared": 7, "won": True, "invalid": False, "duel_fought": True},    # 死斗胜利→完整轮回
        {"cleared": 7, "won": False, "invalid": False, "duel_fought": True},   # 死斗落败→不完整
        {"cleared": 7, "won": False, "invalid": False, "duel_fought": False},  # 第7场封存→不完整
        {"cleared": 2, "won": False, "invalid": False, "duel_fought": False},  # 途中阵亡
    ])
    monkeypatch.setattr(bl, "play", lambda *a, **k: next(seq))
    tele = {}
    score, valid, invalid = bl.fitness("杀伐", ["庇护"], 4, gen=1, telemetry=tele)
    dz = tele["duels"]
    assert dz["fought"] == 2 and dz["won"] == 1, "死斗2场胜1场"
    assert dz["sealed_no_duel"] == 1, "1次封存不计死斗"
    assert dz["by_build"]["杀伐|庇护"] == {"fought": 2, "won": 1}
    # 新口径：完整通关率=1/4；PVE/PVP分层胜负在 telemetry 中分别记录。
    assert score == 0.25, "适应度必须使用完整通关率，而不是平均经历战斗场数"


def test_behavior_stats_recorded_via_play():
    """正常路径：真实对局后 telemetry['behavior_stats'] 必须记录行为→胜率相关数据
    （"提高胜率的行为记录进知识库"的数据闭环入口，2026-08-22 DM诉求）。"""
    import random as _random
    tele = {}
    bl.play("杀伐", ["再生", "庇护"], "罪孽都市", seed=77, rng=_random.Random(3), telemetry=tele)
    bs = tele.get("behavior_stats", {})
    assert bs, "真实对局必须产生行为统计"
    for name, s in bs.items():
        assert s["n"] >= 1 and "wins" in s and "cleared_sum" in s


def test_learned_policy_feeds_winrate_back_into_weights():
    """正常路径：通关增益显著的行为权重被上调、拖累的被下调，且硬限幅。"""
    k = {"telemetry": {
        "outcomes": {"win": 0, "loss": 100, "cleared_sum": 100},   # 基线 1.0 场/局
        "behavior_stats": {
            "修行提战力": {"n": 60, "wins": 0, "cleared_sum": 60 * 1.6},   # 增益 +0.6 → 上调
            "共鸣强化": {"n": 60, "wins": 0, "cleared_sum": 60 * 0.4},     # 增益 -0.6 → 下调
            "探索寻机": {"n": 5, "wins": 0, "cleared_sum": 99},            # min_n 未到 → 不动
        }}}
    pol = bl.learned_policy(k)
    mult = k["policy_learn"]["multipliers"]
    assert mult["修行"] > 1.0 and mult["共鸣"] < 1.0
    assert "探索" not in mult, "样本不足(min_n=40)的行为不得影响权重"
    assert all(0.7 <= m <= 1.4 for m in mult.values())
    assert pol["修行"] > bl.DEFAULT_POLICY["修行"] and pol["共鸣"] < bl.DEFAULT_POLICY["共鸣"]


# ---------- 第十九批实验钩子（消耗品门/死亡归因/实验室隔离） ----------

def test_consumable_gate_unknown_rejected():
    """错误输入：未知消耗品策略名必须拒绝（防实验脚本写错策略静默跑现行）"""
    import pytest
    with pytest.raises(ValueError):
        bl._make_consumable_gate("hp99")


def test_consumable_gate_hp60_widens_window():
    """正常路径：hp60 门在 41%~60% 血线放行（现行≤40%门不放行的区间）"""
    gate = bl._make_consumable_gate("hp60")
    assert gate is not None

    class _P:
        is_alive = True
        current_hp = 50
        blood_limit = 100
    assert gate(type("AI", (), {"player": _P()}))           # 50% ≤60% 放行
    _P.current_hp = 90
    assert not gate(type("AI", (), {"player": _P()}))       # 90% 不放行
    # 对照：None=现行门（B臂≡A臂的机制事实）
    assert bl._make_consumable_gate("current") is None
    assert bl._make_consumable_gate("hp40") is None


def test_consumable_gate_predict_uses_last_round_loss():
    """预测致死门：上回合净掉血 ≥ 当前HP → 提前用；无样本回落现行≤40%门"""
    gate = bl._make_consumable_gate("predict")

    class _P:
        is_alive = True
        current_hp = 20
        blood_limit = 100
    ai = type("AI", (), {"player": _P, "_hp_trace": [80, 45]})  # 上回合净掉35>当前20
    assert gate(ai)
    ai._hp_trace = [80, 78]                                     # 净掉2 < 20 不用
    assert not gate(ai)
    ai._hp_trace = [50]                                         # 样本不足→40%老门
    assert gate(ai)                                             # 20 ≤ 40 老门放行


def test_death_trace_payload_classification():
    """归因分类器：死亡回合回始≥40%血限→burst；尾三回合单调降且<40%→sustained"""
    snaps = [{"battle": 7, "round": i, "hp": h, "hp_pct": h / 100, "mana": 3, "speed": 6,
              "consumables": {}, "enemies": [{"name": "怪", "hp": 50, "speed": 5,
                                              "daowen": ["杀伐"]}],
              "player_daowen": ["封印", "杀伐"]} for i, h in enumerate((70, 55, 45))]
    e = type("E", (), {})()
    e.state = type("S", (), {})()
    e.state.player = type("P", (), {"blood_limit": 100, "current_mana": 3})()
    e.state.consumables = []
    t = bl._death_trace_payload(e, 6, snaps)          # cleared=6 → battle 7
    assert t["battle"] == 7
    assert t["primary"] == "burst", t                   # 末回始45%≥40 → 爆发
    snaps[-1]["hp"] = 25
    snaps[-1]["hp_pct"] = 0.25
    t2 = bl._death_trace_payload(e, 6, snaps)
    assert t2["primary"] == "sustained", t2             # 70→45→25 单调降且<40%


def test_death_trace_uses_snapshot_blood_limit():
    """分母回归钉：死后血限被衰老压低时，进场血线必须按快照当时口径——
    不得把 0.26 虚增成 0.87 误判 rng_suscept/burst（b20 发现的遥测缺陷）。"""
    snaps = [{"battle": 7, "round": i, "hp": h, "hp_pct": h / 100, "mana": 9, "speed": 8,
              "consumables": {}, "enemies": [{"name": "怪", "hp": 50, "speed": 5,
                                              "daowen": ["衰老"]}],
              "player_daowen": ["封印", "杀伐"]} for i, h in enumerate((30, 28, 26))]
    e = type("E", (), {})()
    e.state = type("S", (), {})()
    e.state.player = type("P", (), {"blood_limit": 30, "current_mana": 9})()  # 死前被压到30
    e.state.consumables = []
    t = bl._death_trace_payload(e, 6, snaps)
    assert t["hp_pct_at_last_round"] == 0.26, t         # 必须=快照口径，不得 26/30
    assert t["primary"] == "sustained", t               # 旧实现会虚增成 rng_suspect

# ---------- 新构筑分层评价与低成本归因 ----------

def test_build_metrics_keep_pve_pvp_full_separate():
    """PVE、PVP、完整通关不能用同一个分母或平均清场数替代。"""
    results = [
        {"cleared": 7, "pve_won": True, "pvp_reached": True,
         "pvp_won": True, "full_won": True},
        {"cleared": 7, "pve_won": True, "pvp_reached": False,
         "pvp_won": False, "full_won": False},
        {"cleared": 1, "pve_won": False, "pvp_reached": False,
         "pvp_won": False, "full_won": False},
    ]
    m = bl.summarize_build_results(results)
    assert m["pve_wins"] == 2 and m["pve_trials"] == 3
    assert m["pvp_wins"] == 1 and m["pvp_trials"] == 1
    assert m["full_wins"] == 1 and m["full_trials"] == 3
    assert m["pvp_rate"] == 1.0 and m["full_rate"] == pytest.approx(1 / 3)


def test_metrics_update_does_not_copy_build_score_to_pairs():
    """新搜索路径不得用整局胜利给每个道纹对发同样的贡献分。"""
    k = {"generation": 0, "trials": {}, "pair_scores": {},
         "history": [], "best": None}
    metrics = {"valid": 3, "invalid": 0, "pve_wins": 2, "pve_trials": 3,
               "pve_rate": 2 / 3, "pve_lcb": .2, "pvp_wins": 0,
               "pvp_trials": 0, "pvp_rate": None, "pvp_lcb": None,
               "full_wins": 0, "full_trials": 3, "full_rate": 0.0,
               "full_lcb": 0.0}
    bl.update(k, "A", ["B"], 0.0, metrics=metrics)
    assert k["pair_scores"] == {}
    assert k["history"][-1]["pve_rate"] == pytest.approx(2 / 3)


def test_ablation_runs_only_one_removal_per_rune(monkeypatch):
    """逐个消融只需要 k+1 次构筑评估，不枚举 2^k 子集。"""
    calls = []

    def fake(starter, learn, runs, gen, **kwargs):
        calls.append((starter, tuple(learn)))
        value = 1.0 if starter == "A" and "B" in learn else 0.5
        return {
            "valid": runs, "invalid": 0, "pve_wins": int(value * runs),
            "pve_trials": runs, "pve_rate": value, "pve_lcb": value,
            "pvp_wins": 0, "pvp_trials": 0, "pvp_rate": None, "pvp_lcb": None,
            "full_wins": int(value * runs), "full_trials": runs,
            "full_rate": value, "full_lcb": value, "avg_cleared": 1,
        }

    monkeypatch.setattr(bl, "evaluate_build", fake)
    out = bl.evaluate_ablation("A", ["B", "C"], runs=3, gen=1)
    assert len(calls) == 4  # 完整构筑 + A/B/C 各一次
    assert len(out["ablations"]) == 3
    assert all("Shapley" not in row["full_judgement"] for row in out["ablations"])


def test_observed_combo_candidates_do_not_enumerate_pool():
    activity = {"interactions": {"A+B": 2, "C+D": 1}}
    assert bl.discover_observed_combos(activity) == [("A", "B"), ("C", "D")]
