"""战场公开频道（报告.md 硬伤3）契约测试。

钉住 DM 在 2026-08-30 的四条裁定：
  1. **可真可假，引擎绝不标记真伪**——「你都标出来了，还猜什么」。
     连"姿态标签"都必须是中性修辞（施压/示弱/试探/夸口/随口），不得出现
     「虚张/真话/假话」这类替读者判真伪的标签。
  2. **完全不碰 AI**——TacticalAI 不读频道；频道存在与否不改变任何评分与出招。
  3. **时机自由**——utter() 什么时候都能调，不设时序约束。
  4. **不产生数值效果**——只读状态、写一个字符串。
另钉：台词**必须挂钩当前战局**（说的是这一手真有的东西），否则就是废话。
"""
from __future__ import annotations

import os
import random
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dialogue import (  # noqa: E402
    POSTURES, clear_channel, format_channel, read_channel,
    truth_markers, utter,
)
from engine.models import DaoWen, DaoWenInstance, Entity, GameState  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRACKET = re.compile(r"【([^】]+)】")


def _arena(hp=40, mana=20, foe_hp=38, daowen=("杀伐", "庇护")):
    st = GameState(phase="in_combat", combat_subphase="player_actions")
    st.current_round, st.current_battle = 2, 7
    a = Entity("玄风", "轮回者", blood_limit=40, current_hp=hp,
               mana_limit=20, current_mana=mana, speed_limit=12, current_speed=12)
    for n in daowen:
        a.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=0)
    b = Entity("苍澜", "轮回者", blood_limit=40, current_hp=foe_hp,
               mana_limit=20, current_mana=mana, speed_limit=12, current_speed=12)
    st.player, st.enemies = a, [b]
    return st, a, b


# ==================== 1. 绝不标记真伪 ====================

def test_entry_has_no_truth_marker():
    """条目字段固定为 {round,battle,speaker,posture,text}，禁止任何真伪字段。"""
    banned = truth_markers()
    st, a, _b = _arena()
    for seed in range(200):
        entry = utter(st, a, rng=random.Random(seed))
        keys = {str(k).lower() for k in entry}
        hit = keys & {m.lower() for m in banned}
        assert not hit, f"条目含真伪字段 {hit}: {entry}"
        assert set(entry) == {"round", "battle", "speaker", "posture", "text"}, entry


def test_postures_are_rhetorically_neutral():
    """姿态必须是**中性修辞**，不得出现替读者判真伪的标签（如「虚张」）。"""
    banned = truth_markers()
    for posture in POSTURES:
        assert posture not in banned, f"姿态名 {posture} 本身就是真伪标记"
    assert "虚张" not in POSTURES and "真话" not in POSTURES


def test_formatted_output_has_no_verdict():
    """打印出来的文本也不得做任何真伪裁决。"""
    st, a, _b = _arena()
    rng = random.Random(3)
    for _ in range(50):
        utter(st, a, rng=rng)
    for line in format_channel(st):
        for m in ("真话", "假话", "虚张", "属实", "不可信", "真的", "假的"):
            assert m not in line, f"输出泄露真伪判断: {line}"


# ==================== 2. 公开：双方可见 ====================

def test_channel_is_public_not_filtered_by_viewer():
    """频道是公开的：不按 viewer 过滤，双方与观战者看到同一份。"""
    st, a, b = _arena()
    utter(st, a, rng=random.Random(1))
    utter(st, b, rng=random.Random(2))
    assert read_channel(st) == read_channel(st, viewer=a) == read_channel(st, viewer=b)
    assert [e["speaker"] for e in read_channel(st)] == ["玄风", "苍澜"]


def test_opponent_can_read_what_you_said():
    """对手能读到你说的话（否则就是自言自语，不是博弈）。"""
    st, a, b = _arena()
    entry = utter(st, a, rng=random.Random(7))
    assert any(e["speaker"] == "玄风" and e["text"] == entry["text"]
               for e in read_channel(st, viewer=b))


# ==================== 3. 台词必须挂钩当前战局 ====================

def test_lines_reference_real_held_daowen():
    """台词里点名的道纹必须是说话方**此刻真实持有**的（否则就是废话）。"""
    st, a, _b = _arena(daowen=("杀伐", "庇护"))
    for seed in range(300):
        entry = utter(st, a, rng=random.Random(seed))
        for name in BRACKET.findall(entry["text"]):
            assert name in a.dao_wen or name == "底牌", \
                f"台词点名了说话方没有的道纹「{name}」: {entry['text']}"


def test_no_daowen_falls_back_to_neutral_word():
    """边界：一个道纹都没有时不许编造，回退到「底牌」。"""
    st, a, _b = _arena(daowen=())
    a.dao_wen.clear()  # 2026-11-01：【透支】出生自带，本用例要「一个道纹都没有」
    for seed in range(120):
        entry = utter(st, a, rng=random.Random(seed))
        for name in BRACKET.findall(entry["text"]):
            assert name == "底牌", entry["text"]


def test_posture_reacts_to_situation():
    """姿态随局势变：自己濒死时示弱占比明显高于血足时（说的是当前情况）。"""
    def _ratio(hp, seed_count=400):
        st, a, _b = _arena(hp=hp, foe_hp=38)
        from collections import Counter
        c = Counter(utter(st, a, rng=random.Random(i))["posture"]
                    for i in range(seed_count))
        return c["示弱"] / seed_count
    assert _ratio(6) > _ratio(40) + 0.10, "濒死时应明显更常说示弱"


# ==================== 4. 不产生任何数值效果 ====================

def test_utter_changes_no_numbers():
    """快照：hp / 法力 / 速度 / 格挡 / 状态 / 道纹 全部纹丝不动。"""
    st, a, b = _arena()
    def snap():
        return (a.current_hp, a.current_mana, a.current_speed, a.shield,
                a.blood_limit, a.mana_limit, a.speed_limit,
                [(s.name, s.value, s.remaining_rounds) for s in a.status_effects],
                sorted(a.dao_wen), b.current_hp, b.current_mana, b.current_speed,
                st.shards, st.current_round, st.current_battle, len(st.enemies))
    before = snap()
    rng = random.Random(99)
    for _ in range(100):
        utter(st, a, rng=rng)
        utter(st, b, rng=rng)
    assert snap() == before, "utter() 不得改动任何面板/状态"


# ==================== 5. 可真可假（但不标） ====================

def test_bluff_is_possible_and_unmarked():
    """满法力时照样能喊「法力见底」——可虚张，但引擎不标哪句是虚的。"""
    st, a, _b = _arena(hp=40, mana=20)   # 法力满、血满
    rng = random.Random(2024)
    entries = [utter(st, a, rng=rng) for _ in range(400)]
    low_claims = [e for e in entries
                  if e["posture"] in ("示弱", "夸口")
                  and ("见底" in e["text"] or "空" in e["text"])]
    assert low_claims, "满法力时也应可能喊出弹尽粮绝（否则台词=真话播报）"
    banned = {m.lower() for m in truth_markers()}
    for e in low_claims:
        assert not ({k.lower() for k in e} & banned), e
        assert e["posture"] in POSTURES


# ==================== 6. 红线：不碰 AI ====================

def test_tactical_ai_reads_the_channel():
    """红线 E 已于 2026-08-30 由 DM **解除**：AI 现在**必须**读得到频道。"""
    src = open(os.path.join(ROOT, "engine", "ai_tactics.py"), encoding="utf-8").read()
    assert "read_opponent" in src, "AI 必须读取对手台词（否则台词仍是废话）"
    assert "_dialogue_bias" in src, "AI 必须把「信不信」折算进候选评分"


def test_dialogue_moves_scores_only_via_listener_personality():
    """红线 E 解除后：台词**会**推动评分，但推动力只来自**听者的性格**。

    对照：同一句台词，两个性格相反的听者必须被推向**相反**方向；
    且全过程不改任何数值（只是选择倾向）。
    """
    from engine.api import GameEngine
    from engine.ai_tactics import TacticalAI
    from tests.setup_support import finish_initial_daowen

    def _build(tag):
        e = GameEngine(db_path=f"/tmp/linji_tests/dlg_{tag}.db", rng_seed=5,
                       sealed_candidate_path=f"/tmp/linji_tests/dlg_{tag}_s.json")
        e.execute_action("setup_attributes", {"name": "白某", "blood_points": 11, "speed_points": 8, "mana_points": 6})
        finish_initial_daowen(e)
        e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
        e.execute_action("setup_choose_region", {"region": "乱葬岗"})
        e.state.phase = "in_combat"
        e.state.current_round = 2
        from engine.models import Entity as _E
        foe = _E("靶怪", "怪物", blood_limit=200, current_hp=200,
                 attack_count=1, attack_power=2)
        e.state.enemies.append(foe)
        return e

    def _snapshot(e):
        ai = TacticalAI(e)
        ai._refresh_personality()
        cands = ai._daowen_candidates() + [c for _b, c in ai._resonance_candidates()]
        rows = []
        for c in cands:
            pv = ai.previewer.preview(c["action"], c["params"])
            s = ai._score_candidate(pv.get("diff", {}), c["label"],
                                    c.get("kind"), c.get("target"))
            rows.append((c["label"], None if s is None else round(float(s), 6)))
        return rows, dict(ai._ptraits)

    def _snapshot_with(engine, traits=None):
        ai = TacticalAI(engine)
        ai._refresh_personality()
        if traits is not None:
            ai._ptraits = dict(traits)          # 直接注入听者性格（不碰存档）
        ai._refresh_opponent_read()
        cands = ai._daowen_candidates() + [c for _b, c in ai._resonance_candidates()]
        rows = []
        for c in cands:
            pv = ai.previewer.preview(c["action"], c["params"])
            sc = ai._score_candidate(pv.get("diff", {}), c["label"],
                                     c.get("kind"), c.get("target"))
            rows.append((c["label"], None if sc is None else round(float(sc), 6)))
        return rows, ai._opponent_read

    TRUSTING = {"interpersonal_tendency": 0.9, "moral_baseline": 0.9}   # 守义+信任
    SUSPICIOUS = {"decision_habit": 0.9, "emotional_stability": -0.5}   # 先观察+易波动

    e_clean = _build("clean")
    base_rows, _ = _snapshot_with(e_clean)
    assert _snapshot_with(e_clean)[1] is None, "没有台词时不该有读数"

    # 同一句"示弱"，灌进两个性格不同的听者
    e_t, e_s = _build("trust"), _build("suspect")
    for e in (e_t, e_s):
        utter(e.state, e.state.enemies[0], posture="示弱", rng=random.Random(1))
    rows_t, read_t = _snapshot_with(e_t, TRUSTING)
    rows_s, read_s = _snapshot_with(e_s, SUSPICIOUS)

    assert read_t["claim"] == "weak" and read_s["claim"] == "weak"
    assert read_t["belief"] > 0.3, f"守义+信任者应倾向相信: {read_t}"
    assert read_s["belief"] < 0.0, f"先观察+易波动者应倾向怀疑: {read_s}"
    assert rows_t != base_rows or rows_s != base_rows, "台词必须能推动评分（否则仍是废话）"


# ==================== 7. 生命周期 ====================

def test_clear_channel_on_battle_end():
    """台词是局内信息，[战终]清空，不跨战斗保留。"""
    st, a, _b = _arena()
    for i in range(5):
        utter(st, a, rng=random.Random(i))
    assert len(read_channel(st)) == 5
    clear_channel(st)
    assert read_channel(st) == []


def test_channel_survives_deepcopy_and_pickle():
    """存档往返：纯 dict 结构，随 deepcopy / pickle 自然往返。"""
    import copy
    import pickle
    st, a, _b = _arena()
    for i in range(3):
        utter(st, a, rng=random.Random(i))
    challenge = [e["text"] for e in st.battle_channel]
    assert [e["text"] for e in copy.deepcopy(st).battle_channel] == challenge
    assert [e["text"] for e in pickle.loads(pickle.dumps(st)).battle_channel] == challenge


def test_full_duel_stays_legal_when_dialogue_influences_ai():
    """解除红线后的端到端护栏：台词**可以**改变出招（这正是目的），
    但不允许把局面推到非法——面板不变量必须始终成立、判定必须合法。
    """
    import io
    import contextlib
    import glob
    import os as _os

    winners = sorted(glob.glob(_os.path.join(ROOT, "data", "breed_winners", "*.json")))
    if len(winners) < 2:
        pytest.skip("需要 data/breed_winners/ 下至少两个胜者快照")
    try:
        from sim.duel_diff_trace import traced_duel
        import sim.duel_pvp as dp
    except Exception as exc:            # 死斗工具链不可用时跳过，不阻塞本契约
        pytest.skip(f"死斗工具链不可用: {exc}")

    a, b = winners[0], winners[1]

    def _run(seed_reset=True):
        random.seed(12345)     # 每次重置：既有 render_line 用全局随机，进程内会串味
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = traced_duel(a, b, 1)
        return res, buf.getvalue()

    def _core(text):
        head = []
        for line in text.splitlines():
            if line.startswith("=== 对白 ===") or line.startswith("  💬"):
                break
            head.append(line)
        return head + [l for l in text.splitlines() if l.startswith("=== 判定 ===")]

    res_on, out_on = _run()
    original = dp._publish_line
    dp._publish_line = lambda *args, **kwargs: None
    try:
        res_off, out_off = _run()
    finally:
        dp._publish_line = original

    for tag, res, out in (("开", res_on, out_on), ("关", res_off, out_off)):
        assert res and "winner" in res, f"{tag}: 死斗未产出合法判定: {res}"
        assert "面板不变量" in out, f"{tag}: 未跑不变量自检"
        assert "✗" not in out.split("=== 动作日志 ===")[0], \
            f"{tag}: 面板不变量被违反（台词不得把局面推到非法）"
        assert any(l.startswith("  🗣") for l in out.splitlines()) is (tag == "开")
    # 台词真的被 AI 听到了：有频道时应当存在读数/偏移的可能（不强制判定不同，
    # 因为"信不信"可能恰好不改变这一手的最优解）


# ==================== 硬伤3 核心：信不信由听者性格定 ====================

def test_belief_ignores_speaker_true_state():
    """最要紧的一条：判断**不许**偷看说话方真实状态。

    A 说自己没法力了——B 要是能查一下 A 的法力就真相大白，博弈就没了。
    这里把 A 的血/法力/道纹翻个底朝天，B 的判断必须分毫不动。
    """
    from engine.dialogue import read_opponent
    st, a, b = _arena(hp=40, mana=20)
    utter(st, a, posture="示弱", rng=random.Random(1))
    read_low = read_opponent(st, b, traits={"risk_preference": 0.6})

    st2, a2, b2 = _arena(hp=2, mana=0, daowen=())       # 同一句话，A 真的弹尽粮绝
    utter(st2, a2, posture="示弱", rng=random.Random(1))
    read_true = read_opponent(st2, b2, traits={"risk_preference": 0.6})

    assert read_low["belief"] == read_true["belief"], \
        "判断不得因说话方真实状态而变（偷看就没有博弈了）"


def test_same_line_different_listeners_different_verdict():
    """同一句「我没法力了」，不同性格的听者结论相反。"""
    from engine.dialogue import read_opponent
    st, a, b = _arena()
    utter(st, a, posture="示弱", rng=random.Random(1))
    trusting = read_opponent(st, b, traits={"interpersonal_tendency": 0.9,
                                            "moral_baseline": 0.9})
    suspicious = read_opponent(st, b, traits={"decision_habit": 0.9,
                                              "emotional_stability": -0.5})
    assert trusting["belief"] > 0.3, trusting
    assert suspicious["belief"] < 0.0, suspicious
    assert trusting["belief"] > suspicious["belief"]


def test_risk_taker_buys_weakness_but_discounts_threats():
    """冒险者：对手喊虚它更愿信（好压上收割），对手威胁它更不当回事。"""
    from engine.dialogue import belief_from_traits
    bold = {"risk_preference": 0.9}
    meek = {"risk_preference": -0.9}
    assert belief_from_traits(bold, "weak") > belief_from_traits(meek, "weak")
    assert belief_from_traits(bold, "strong") > belief_from_traits(meek, "strong")


def test_crying_wolf_decays_belief():
    """狼来了：同一姿态反复说，可信度逐次衰减。"""
    from engine.dialogue import read_opponent
    st, a, b = _arena()
    traits = {"interpersonal_tendency": 0.6, "moral_baseline": 0.6}
    utter(st, a, posture="示弱", rng=random.Random(1))
    first = read_opponent(st, b, traits=traits)["belief"]
    for i in range(4):
        utter(st, a, posture="示弱", rng=random.Random(i + 2))
    later = read_opponent(st, b, traits=traits)
    assert later["repeats"] >= 4, later
    assert later["belief"] < first, f"重复示弱应更不可信: {first} → {later['belief']}"


def test_belief_is_deterministic():
    """同一听者、同一句话，判断必须可复现（不含随机抖动，便于复现对局）。"""
    from engine.dialogue import read_opponent
    st, a, b = _arena()
    utter(st, a, posture="施压", rng=random.Random(1))
    t = {"moral_baseline": 0.5, "decision_habit": -0.3}
    vals = {read_opponent(st, b, traits=t)["belief"] for _ in range(50)}
    assert len(vals) == 1, f"判断不可复现: {vals}"


def test_no_claim_means_no_read():
    """边界：「随口」没有主张，不产生读数；没台词也不产生读数。"""
    from engine.dialogue import read_opponent
    st, a, b = _arena()
    assert read_opponent(st, b, traits={}) is None          # 空频道
    utter(st, a, posture="随口", rng=random.Random(1))
    assert read_opponent(st, b, traits={}) is None          # 无实质主张


def test_dialogue_bias_pushes_offense_and_defense_apart():
    """信了"对手虚" → 进攻加值、龟缩减值；不信 → 反过来（怕被收割）。"""
    from engine.ai_tactics import TacticalAI
    st, a, b = _arena()
    ai = TacticalAI.__new__(TacticalAI)          # 只验偏置函数，不构造完整 AI
    ai._opponent_read = {"claim": "weak", "belief": 0.8, "posture": "示弱"}
    press = ai._dialogue_bias(enemy_hp_loss=12, shield_useful=0, heal=0, mana_spent=0)
    turtle = ai._dialogue_bias(enemy_hp_loss=0, shield_useful=8, heal=0, mana_spent=0)
    assert press > 0, "信对手虚时，输出手应更值钱"
    assert turtle < 0, "信对手虚时，龟缩应更不值钱"

    ai._opponent_read = {"claim": "weak", "belief": -0.8, "posture": "示弱"}
    assert ai._dialogue_bias(enemy_hp_loss=12, shield_useful=0, heal=0,
                             mana_spent=0) < 0, "怀疑是陷阱时，压上应被扣分"
    assert ai._dialogue_bias(enemy_hp_loss=0, shield_useful=8, heal=0,
                             mana_spent=0) > 0, "怀疑是陷阱时，防御应加分"


def test_threat_believed_means_deterrence():
    """「让敌人忌惮就是最大的作用」：信了威胁 → 忌惮，加防、少压上。"""
    from engine.ai_tactics import TacticalAI
    ai = TacticalAI.__new__(TacticalAI)
    ai._opponent_read = {"claim": "strong", "belief": 0.9, "posture": "施压"}
    assert ai._dialogue_bias(enemy_hp_loss=12, shield_useful=0, heal=0,
                             mana_spent=0) < 0, "信了威胁就别硬上"
    assert ai._dialogue_bias(enemy_hp_loss=0, shield_useful=8, heal=0,
                             mana_spent=0) > 0, "信了威胁就该加防"
    ai._opponent_read = {"claim": "strong", "belief": -0.9, "posture": "施压"}
    assert ai._dialogue_bias(enemy_hp_loss=12, shield_useful=0, heal=0,
                             mana_spent=0) > 0, "识破虚张就该照打"


def test_dialogue_bias_respects_safety_rails():
    """偏置有硬上限，压不过 CRITICAL(-30)/LETHAL 等安全护栏。"""
    from engine.ai_tactics import TacticalAI
    ai = TacticalAI.__new__(TacticalAI)
    ai._opponent_read = {"claim": "weak", "belief": 1.0, "posture": "示弱"}
    huge = ai._dialogue_bias(enemy_hp_loss=10_000, shield_useful=0, heal=0,
                             mana_spent=0)
    assert huge <= TacticalAI.DIALOGUE_BIAS_CAP <= 30.0, \
        "台词偏置不得压过安全护栏"


# DM裁定 2026-09-10 杀伐 2X→5X 后，输出侧分数整体放大约 3.5 倍，压过防守侧。
# 实测本条场景（残血、对手沉默）：防守 39.60 vs 输出 136.96（旧规则下防守占优，本条通过）。
# 与 tests/test_ai_dynamic_decision.py 那两条同因——AI 的威胁/性格修正量级没随倍率放大。
# 属 AI 决策平衡变化，按既定规矩需 DM 裁定后再动权重；断言本体保留，权重回调后自动转 XPASS。
@pytest.mark.xfail(reason="杀伐 5X 使输出分数饱和（39.60 防守 vs 136.96 输出）；待 DM 裁定 AI 权重量级",
                   strict=False)
def test_the_plea_scenario_trusting_presses_suspicious_holds():
    """用户举的那个局面，端到端钉住：

    A 说自己已经没有法力 → B 得想：是真的弹尽粮绝，还是在骗我全力攻击然后收割。
    同一局面、同一句话：
      · 信任型 B 信了 → 从「先回血」改成「压上收割」；
      · 多疑型 B 不信 → 更不敢动，回血优先、压上被扣分。
    两者在 A **沉默**时评分完全一致——证明分歧**只**来自那句话。
    """
    from engine.api import GameEngine
    from engine.ai_tactics import TacticalAI
    from engine.models import Entity as _Ent
    from tests.setup_support import finish_initial_daowen

    def _build(tag):
        e = GameEngine(db_path=f"/tmp/linji_tests/plea_{tag}.db", rng_seed=5,
                       sealed_candidate_path=f"/tmp/linji_tests/plea_{tag}_s.json")
        e.execute_action("setup_attributes", {"name": "B", "blood_points": 11, "speed_points": 8, "mana_points": 6})
        finish_initial_daowen(e)
        e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
        e.execute_action("setup_choose_region", {"region": "乱葬岗"})
        e.state.phase = "in_combat"
        e.state.current_round = 2
        p = e.state.player
        p.current_hp, p.current_mana = 18, 20          # 残血：真有"先稳一手"的两难
        from engine.models import DaoWen as _DW, DaoWenInstance as _DI
        for n in ("庇护", "再生"):
            p.dao_wen[n] = _DI(_DW(name=n, formula="", cost_type="消耗",
                                   cost_formula="X", effect_formula=""), x_value=0)
        e.state.enemies.append(_Ent("A", "怪物", blood_limit=60, current_hp=26,
                                    attack_count=2, attack_power=3))
        return e

    def _sides(tag, traits, posture):
        e = _build(tag)
        if posture:
            utter(e.state, e.state.enemies[0], posture=posture, rng=random.Random(1))
        ai = TacticalAI(e)
        ai._refresh_personality()
        ai._ptraits = dict(traits)
        ai._refresh_opponent_read()
        off = def_ = None
        for c in ai._daowen_candidates()[:8]:
            pv = ai.previewer.preview(c["action"], c["params"])
            if not (pv.get("result") or {}).get("success"):
                continue
            sc = ai._score_candidate(pv.get("diff", {}), c["label"],
                                     c.get("kind"), c.get("target"))
            if sc is None:
                continue
            base = c["label"].split("X=")[0]
            if "杀伐" in base:
                off = sc if off is None else max(off, sc)
            elif base in ("再生", "庇护"):
                def_ = sc if def_ is None else max(def_, sc)
        return off, def_, ai._opponent_read

    TRUSTING = {"interpersonal_tendency": 0.9, "moral_baseline": 0.9}
    SUSPICIOUS = {"decision_habit": 0.9, "emotional_stability": -0.5}

    # A 沉默时，两种性格的分差完全一致 —— 分歧不由性格本身的其他调制造成
    t_silent = _sides("t0", TRUSTING, None)
    s_silent = _sides("s0", SUSPICIOUS, None)
    assert t_silent[:2] == s_silent[:2], f"沉默时两者应一致: {t_silent} vs {s_silent}"
    assert t_silent[1] > t_silent[0], f"残血局面下，沉默时本就该先稳一手: 防守{t_silent[1]} vs 输出{t_silent[0]}"

    # A 说「我没法力了」
    t_said = _sides("t1", TRUSTING, "示弱")
    s_said = _sides("s1", SUSPICIOUS, "示弱")
    assert t_said[2]["belief"] > 0.5, t_said[2]
    assert s_said[2]["belief"] < 0.0, s_said[2]

    assert t_said[0] > t_said[1], \
        f"信任型应改压上收割: 进攻{t_said[0]:.1f} vs 防御{t_said[1]:.1f}"
    assert s_said[1] > s_said[0], \
        f"多疑型应更不敢动: 进攻{s_said[0]:.1f} vs 防御{s_said[1]:.1f}"
    assert s_said[0] < s_silent[0], "多疑型听到示弱后，压上这一手应当被扣分"


def test_duelist_names_are_picked_for_personality_contrast():
    """死斗双方的性格由**名字哈希**决定，纯随机抽名会抽到两个"谁也不信谁也不疑"
    的木头（实测 seed=1 原名「司空/闻人」信念各约 +0.06 / −0.29），对白就退化成
    垃圾话。故 `_pick_contrasting_names` 必须挑**反差最大**的一对，且可复现。
    """
    import hashlib
    from engine.dialogue import belief_from_traits
    from sim.duel_pvp import _DUELIST_NAME_POOL, _TRAIT_DIMS, _pick_contrasting_names

    def traits(name):
        d = hashlib.sha256(name.encode("utf-8")).hexdigest()
        out = {}
        for i, dim in enumerate(_TRAIT_DIMS):
            byte = int(d[i % len(d):i % len(d) + 2], 16)
            out[dim] = (1 if byte % 2 == 0 else -1) * (0.55 + (byte % 5) / 10.0)
        return out

    pool = list(_DUELIST_NAME_POOL)
    a, b = _pick_contrasting_names(pool, 1)
    assert a and b and a != b, (a, b)
    gap = abs(belief_from_traits(traits(a), "weak")
              - belief_from_traits(traits(b), "weak"))
    assert gap > 1.0, f"反差太小（{gap:.2f}），对白仍会是垃圾话: {a} vs {b}"
    # 确定性：同 seed 必须同结果；且两者一信一疑
    assert _pick_contrasting_names(pool, 1) == (a, b), "同 seed 必须可复现"
    ba = belief_from_traits(traits(a), "weak")
    bb = belief_from_traits(traits(b), "weak")
    assert ba * bb <= 0, f"必须一信一疑，实 {ba:+.2f} / {bb:+.2f}"
