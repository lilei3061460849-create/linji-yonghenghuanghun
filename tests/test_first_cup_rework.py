"""【第一杯】重做（2026-09-23 用户令）：你受到的[回复]与失去的生命翻倍。

规则变更（唯一事实源：`物品索引.md#第一杯`）：
    旧：获得的每一次[回复]额外+50%；每个[回终]若本回合未获得过[回复]，流血10；
        你不再受到"癌变"事件的影响。
    新：你受到的[回复]与失去的生命翻倍。

本文件覆盖：
1. 回复翻倍（持有者本人；朋友/员工不继承；无遗物者行为与旧口径逐位相同）；
2. 失去的生命翻倍（伤害/格挡交互、数值型代价【流血】、直接失血、血限与当前生命同时扣减）；
3. 不再是免疫癌变——持有者照样癌变，且因回复翻倍更快撞上 2×[血限]；
4. 明确**不**翻倍的两类：血限压顶造成的当前生命下降、把当前生命直接置0的命零类效果；
5. 失血总账记的是翻倍后的真实失去量（承露盏/「失去生命后」反应据此结算）。

运行：
    .venv/bin/python -m pytest tests/test_first_cup_rework.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Relic, DaoWen, DaoWenInstance, Entity, StatusEffect
from tests.setup_support import finish_initial_daowen


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_firstcup_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 3
    return engine


def _give_daowen(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


def _start_with_enemy(engine, enemy) -> None:
    """进入战斗回合内，并把自动出怪换成受控靶怪。"""
    engine.state.energy = 0
    choices = {r.name: {"use": False} for r in engine.state.relics}
    engine.execute_action("battle_start", {"relic_choices": choices})
    engine.state.enemies.clear()
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})


def _cup_engine(db_suffix: str, daowen="杀伐", enemy=None):
    """带【第一杯】的战斗内引擎；默认靶怪 100 血、无道纹、不还手。"""
    engine = _new_engine(db_suffix)
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    if daowen:
        _give_daowen(engine.state.player, daowen)
    _start_with_enemy(engine, enemy or Entity(
        name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))
    return engine


# ==================== 1. 回复翻倍 ====================

def test_first_cup_doubles_received_heal():
    """持有者受到的[回复]翻倍：面板、明细、累计回复量三处一致。"""
    engine = _cup_engine("heal_ok")
    player = engine.state.player
    player.current_hp = 10

    detail = engine.state.apply_heal(player, 7)

    assert detail["heal_amount"] == 14, "回复量应翻倍"
    assert detail["actual_heal"] == 14
    assert player.current_hp == 24
    assert player.total_healed == 14, "累计回复量（癌变阈值）必须记翻倍后的真实值"


def test_first_cup_heal_double_is_capped_by_blood_limit():
    """翻倍不是穿透上限：回复仍受[血限]封顶，过量部分照常计入累计回复量。"""
    engine = _cup_engine("heal_cap")
    player = engine.state.player
    player.current_hp = player.blood_limit - 3

    detail = engine.state.apply_heal(player, 10)   # 翻倍 20，但只剩 3 点空间

    assert player.current_hp == player.blood_limit
    assert detail["actual_heal"] == 3
    assert detail["overheal"] == 17
    assert player.total_healed == 20


def test_first_cup_does_not_double_ally_heal():
    """朋友/员工不继承遗物：只有持有者本人翻倍。"""
    engine = _cup_engine("heal_ally")
    friend = Entity(name="同伴", entity_type="朋友", blood_limit=40, current_hp=10)
    engine.state.friends.append(friend)

    engine.state.apply_heal(friend, 7)

    assert friend.current_hp == 17, "非持有者回复量不变"


def test_without_first_cup_heal_is_unchanged():
    """回归护栏：无遗物时回复口径与旧版本逐位相同。"""
    engine = _new_engine("heal_plain")
    player = engine.state.player
    player.current_hp = 10

    engine.state.apply_heal(player, 7)

    assert player.current_hp == 17
    assert player.total_healed == 7


# ==================== 2. 失去的生命翻倍 ====================

def test_first_cup_doubles_damage_life_loss():
    """伤害：失去的生命翻倍，失血总账同步记翻倍后的数值。"""
    engine = _cup_engine("dmg_ok")
    player, monster = engine.state.player, engine.state.enemies[0]
    hp_before = player.current_hp

    detail = engine.combat._apply_hostile_damage(
        player, 6, "普通", source=monster,
        ctx={"timing": "test", "source": "测试伤害", "source_type": "test",
             "actor": monster, "target": player, "mechanic": "damage"})

    assert detail["actual_damage"] == 12, "6 点伤害应造成 12 点生命损失"
    assert player.current_hp == hp_before - 12
    assert player._hp_loss_events[-1]["amount"] == 12, "失血总账不得少记一半"


def test_first_cup_doubling_happens_after_shield():
    """翻倍的是“失去的生命”，不是原始伤害：格挡该吸多少还是多少。"""
    engine = _cup_engine("dmg_shield")
    player, monster = engine.state.player, engine.state.enemies[0]
    player.gain_shield(4)
    hp_before = player.current_hp

    detail = engine.combat._apply_hostile_damage(
        player, 10, "普通", source=monster,
        ctx={"timing": "test", "source": "测试伤害", "source_type": "test",
             "actor": monster, "target": player, "mechanic": "damage"})

    assert detail["shield_absorbed"] == 4, "格挡仍按原始伤害吸收 4"
    assert detail["actual_damage"] == 12, "落地 6 点生命损失，翻倍为 12"
    assert player.current_hp == hp_before - 12
    assert player.shield == 0


def test_first_cup_doubles_bleed_cost_life_loss():
    """数值型代价【流血X】：代价按 X 支付，失去的生命翻倍。"""
    engine = _cup_engine("cost_ok", daowen="连击")
    player = engine.state.player
    player.current_mana = 50
    hp_before = player.current_hp

    engine.execute_action("use_daowen", {"daowen_name": "连击", "x": 5, "target": "靶怪"})

    assert player.current_hp == hp_before - 50, "流血25（连击X=5）的代价应造成 50 点生命损失"
    assert player._hp_loss_events[-1]["amount"] == 50


def test_first_cup_doubles_raw_hp_loss():
    """直接失血（爆裂反噬/遗物【灾厄轮盘】/血影同口径）同样翻倍。"""
    engine = _cup_engine("raw_ok")
    player = engine.state.player
    hp_before = player.current_hp

    result = engine.combat._raw_hp_loss(player, 4)

    assert result["lost"] == 8
    assert player.current_hp == hp_before - 8


def test_first_cup_doubles_qianjingjia_reflect():
    """千荆甲反噬（2026-10-03 承接被删【爆裂】）：攻击者失去的生命翻倍。

    反噬只能由**轮回者持有者**打出（怪物不持遗物），故靶子换成敌方轮回者，
    并把【千荆甲】放进 opponent_relics。反噬本身是直接失血，倍率仍走
    life_loss_multiplier（Hook 与记账共用同一事实源）。
    """
    engine = _cup_engine("qianjingjia")
    player = engine.state.player
    holder = Entity(name="持甲者", entity_type="轮回者", blood_limit=200, current_hp=200,
                    mana_limit=10, current_mana=10)
    engine.state.enemies.append(holder)
    engine.state.opponent_relics.append(Relic(name="千荆甲", effect=""))
    hp_before = player.current_hp

    engine.combat._apply_hostile_damage(
        holder, 5, "普通", source=player,
        ctx={"timing": "test", "source": "测试伤害", "source_type": "test",
             "actor": player, "target": holder, "mechanic": "damage",
             "subtype": "attack"})

    assert player.current_hp == hp_before - 10, "5 点反噬应造成 10 点生命损失"


def test_first_cup_doubles_hp_reduction_in_daowen_calc():
    """规则明写「[血限]及当前生命同时 -NX」的结算：当前生命那部分翻倍、[血限]不翻倍。

    说明：`hp_reduction` 是道纹计算的既有键（`engine/combat.py` 白名单），
    当前没有在产道纹产出它，因此这里用合成 calc 直接验证接线（规则一旦有产出，
    倍率已经在位）。合成 calc 只带这两个键，不涉及消耗与其它分支。
    """
    engine = _cup_engine("hp_reduction")
    player = engine.state.player
    hp_before, bl_before = player.current_hp, player.blood_limit

    engine.combat.apply_daowen_effect(
        "杀伐", {"blood_limit_reduction": 4, "hp_reduction": 8}, player, player)

    assert player.blood_limit == bl_before - 4, "[血限]扣减不翻倍"
    assert player.current_hp == hp_before - 16, "当前生命扣减翻倍（8→16）"


def test_without_first_cup_damage_is_unchanged():
    """回归护栏：无遗物时伤害口径与旧版本逐位相同。"""
    engine = _new_engine("dmg_plain")
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))
    player, monster = engine.state.player, engine.state.enemies[0]
    hp_before = player.current_hp

    detail = engine.combat._apply_hostile_damage(
        player, 6, "普通", source=monster,
        ctx={"timing": "test", "source": "测试伤害", "source_type": "test",
             "actor": monster, "target": player, "mechanic": "damage"})

    assert detail["actual_damage"] == 6
    assert player.current_hp == hp_before - 6
    assert "life_loss_multiplier" not in detail


# ==================== 3. 不再是免疫癌变 ====================

def test_first_cup_no_longer_blocks_cancer():
    """旧「免疫癌变」条款废止：持有者照样癌变（钱袋并入的那条效果已随重做删除）。"""
    engine = _cup_engine("cancer")
    player = engine.state.player
    player.total_healed = engine.combat.cancer_threshold_of(player)

    hit = engine.combat.check_cancer(player)

    assert hit is not None, "【第一杯】不再提供癌变免疫"
    assert not player.is_alive


def test_first_cup_cancer_threshold_is_unchanged():
    """阈值本身没动：仍是本场累计回复 ≥ 2×[血限]（翻倍来自回复量，不是阈值）。"""
    engine = _cup_engine("cancer_th")
    player = engine.state.player

    assert engine.combat.cancer_threshold_of(player) == player.blood_limit * 2
    player.total_healed = player.blood_limit * 2 - 1
    assert engine.combat.check_cancer(player) is None


def test_first_cup_reaches_cancer_twice_as_fast():
    """回复翻倍 ⇒ 同样的治疗量把持有者更快推到癌变线。"""
    engine = _cup_engine("cancer_fast")
    player = engine.state.player
    player.current_hp = 1
    line = engine.combat.cancer_threshold_of(player)

    engine.state.apply_heal(player, line // 2)      # 翻倍后恰好到线

    assert player.total_healed == line
    assert engine.combat.check_cancer(player) is not None


# ==================== 4. 明确不翻倍的两类 ====================

def test_first_cup_does_not_double_blood_limit_clamp():
    """[血限]被压低导致的当前生命封顶：不是“失去生命”，不翻倍（见报告的口径说明）。"""
    engine = _cup_engine("clamp")
    player = engine.state.player
    player.current_hp = player.blood_limit      # 满血：血限一降，压顶立刻生效
    bl_before = player.blood_limit

    engine.combat._apply_blood_limit_change(
        player, -10, "测试衰老", "debuff", ctx={"timing": "test", "source": "测试衰老",
                                             "source_type": "test", "mechanic": "blood_limit"})

    assert player.blood_limit == bl_before - 10
    assert player.current_hp == bl_before - 10, "压顶只压到新[血限]，不按翻倍再补一刀"


def test_first_cup_death_zeroing_is_idempotent():
    """把当前生命直接置0的命零类效果（癌变/迷失/雕塑）不带数值，翻倍无从作用。"""
    engine = _cup_engine("zero")
    player = engine.state.player
    player.current_hp = 3

    engine.combat._raw_hp_loss(player, 4)      # 4 → 翻倍 8，但生命只有 3

    assert player.current_hp == 0
    assert not player.is_alive, "过量失血照常命零，不得出现 0 血存活"


# ==================== 5. 预览一致性（不得只在正式结算里翻倍） ====================

def test_first_cup_doubling_visible_in_preview():
    """预演与正式执行同口径：预演里也翻倍，且预演不得污染真实状态。

    倍率的唯一事实源挂在 GameState 上（`side_has` 走状态自身的 relics/player），
    沙盒副本里判定同样成立。若把它做成"引擎侧 + 真实对象身份"判断，
    预演就会少算一半生命损失，AI 会据此做出错误决策。
    """
    from engine.ai_preview import ActionPreview

    def _preview(cup: bool, db_suffix: str):
        # 2026-10-03：原用例借【爆裂】反噬制造「自己失去的生命」，该道纹已删除；
        # 改用【连击】：代价流血X（自己失去 X 点生命）+ 对目标造成 X 点伤害，
        # 同一预演里同时可观察「打出的伤害」与「自己失去的生命」。
        engine = _cup_engine(db_suffix, daowen="连击") if cup else _new_engine(db_suffix)
        if not cup:
            _give_daowen(engine.state.player, "连击")
            _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物",
                                             blood_limit=200, current_hp=200))
        monster = engine.state.enemies[0]
        monster.blood_limit = monster.current_hp = 200
        player = engine.state.player
        player.current_mana = 40
        player.current_hp = 100
        engine.state.shards = 0
        player_hp_before, monster_hp_before = player.current_hp, monster.current_hp
        preview = ActionPreview(engine).preview(
            "use_daowen", {"daowen_name": "连击", "x": 3, "target": "靶怪"})
        diff = preview["diff"]
        assert preview["result"].get("error") is None
        # 预演零污染
        assert player.current_hp == player_hp_before
        assert monster.current_hp == monster_hp_before
        return (diff["player"]["hp_before"] - diff["player"]["hp_after"],
                diff["enemies"][0]["hp_before"] - diff["enemies"][0]["hp_after"])

    plain_loss, plain_dealt = _preview(False, "pvw_plain")
    cup_loss, cup_dealt = _preview(True, "pvw_cup")

    assert cup_dealt == plain_dealt, "打出去的伤害不变（翻的只是自己失去的生命）"
    assert plain_loss == plain_dealt, "无遗物：流血代价 = 打出的伤害（连击X=3 → 各 3 点）"
    assert cup_loss == plain_loss * 2, "持有【第一杯】：预演里的生命损失同样翻倍"
