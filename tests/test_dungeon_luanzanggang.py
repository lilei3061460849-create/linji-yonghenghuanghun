"""乱葬岗（二阶副本）实现契约测试。

覆盖：4条幸存的乱葬岗专属道纹、遗物化空位的闭环保留、区域可选、怪物池解析、附煞行动与幸存道纹效果。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine, RESERVED_SLOT, RELICIZED_DAOWEN_SLOTS
from engine.models import Entity, GameState
from engine.dice import DiceEngine
from engine.combat import CombatEngine


# ---------- 道纹注册与闭环 ----------

def test_dungeon_daowen_registered():
    """正常路径：4个幸存的乱葬岗专属道纹已注册并可解析。

    2026-10-03 删【尸爆】；2026-10-08 删【镇尸】（与【坏死】硬重复）。
    """
    DaoWenEngine.register_all()
    for name in ("分裂", "瓦解", "勾魂", "招魂"):
        assert name in DaoWenEngine._registry, f"{name} 未注册"
        r = DaoWenEngine.resolve(name, 2, target=Entity("T", "怪物", blood_limit=100, current_hp=100),
                                 caster=Entity("C", "轮回者", blood_limit=60, current_hp=60))
        assert isinstance(r, dict) and r["dao_wen"] == name


def test_dungeon_resonance_loop_complete():
    """正常路径：乱葬岗残韵闭环7条路径全部可达。

    遗物化的【缄默】【冥气】各自保留为独立「？？」节点，不与相邻道纹直连。
    2026-10-08 删【镇尸】：勾魂与招魂之间那一格留成**空占位符**，环仍是 7 条边，
    因此这两条路径查得到，但不可获得（见 test_reserved_slot_not_obtainable）。
    """
    DaoWenEngine.register_all()
    slots = RELICIZED_DAOWEN_SLOTS
    expected_edges = [
        ("分裂", "转换", slots["缄默"]),
        (slots["缄默"], "曲解", "瓦解"),
        ("瓦解", "转换", slots["冥气"]),
        (slots["冥气"], "反转", "勾魂"),
        ("勾魂", "曲解", RESERVED_SLOT),
        (RESERVED_SLOT, "曲解", "招魂"),
        ("招魂", "转换", "分裂"),
    ]
    edges = ResonanceEngine.CLOSED_LOOPS["乱葬岗闭环"]
    assert len(edges) == 7
    for edge in expected_edges:
        assert edge in edges, f"乱葬岗闭环缺少边 {edge}"
    sources = {src for src, _, _ in edges}
    targets = {dst for _, _, dst in edges}
    assert sources == targets, "闭环每个节点都必须恰有前后相邻边"
    assert all(name not in DaoWenEngine._registry for name in slots.values())


def test_reserved_slot_not_obtainable():
    """边界：闭环上的空占位符查得到，但**不能获得**——残韵走到它=无事发生。

    不消耗残韵、不改写目标牌面、不授予施法者（2026-10-08 用户令）。
    """
    DaoWenEngine.register_all()
    stock = {"曲解": 3}
    r = ResonanceEngine.apply_resonance("勾魂", "曲解", True, True, dict(stock))
    assert r["success"] is False
    assert r.get("reserved") is True
    assert stock["曲解"] == 3, "走到空占位符不得消耗残韵"
    # 显式指定空位同样被拒
    r2 = ResonanceEngine.apply_resonance("招魂", "曲解", True, True, dict(stock),
                                         target_daowen=RESERVED_SLOT)
    assert r2["success"] is False and r2.get("reserved") is True
    assert stock["曲解"] == 3
    # 空位不是道纹：不得出现在注册表与任何持有集合中
    assert RESERVED_SLOT not in DaoWenEngine._registry
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, MONSTER_TRANSFORM_DAOWEN
    assert RESERVED_SLOT not in REGION_EXCLUSIVE_DAOWEN.get("乱葬岗", set())
    assert RESERVED_SLOT not in MONSTER_TRANSFORM_DAOWEN


# ---------- 区域与怪物池 ----------

def test_dungeon_selectable_region():
    """正常路径：乱葬岗可作为开局副本选择。"""
    e = GameEngine(db_path="/tmp/test_lz_region.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    r = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    assert r["success"], r
    assert e.state.current_region == "乱葬岗"


def test_dungeon_monster_pool_parsed():
    """正常路径：乱葬岗12只怪物被解析为运行时怪物源。"""
    from engine.monsters import parse_monster_pool
    pools = parse_monster_pool("副本索引.md")
    assert len(pools["乱葬岗"]) == 12
    names = {m["name"] for m in pools["乱葬岗"]}
    assert {"蛆冢", "纸人", "勾魂使者", "血僵"} <= names


# ---------- 旧附煞行动已删除 ----------

def test_fusha_action_is_removed_in_favor_of_creation_system():
    """旧附煞菜单已删除；造生/造物承接其设计位置，不再给普通道纹附加煞气。"""
    e = GameEngine(db_path="/tmp/test_lz_fusha_removed.db", rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    e.state.shards = 200

    result = e.execute_action("pre_battle_action", {"sub_action": "附煞"})

    assert not result["success"]
    assert "未知局外行动" in result["error"]
    assert e.state.shards == 200
    assert not e.state.pending_sha_qi_choices
    assert all(not instance.sha_qi for instance in e.state.player.dao_wen.values())


# ---------- 专属道纹效果 ----------

def test_wajie_reduces_blood_limit_pct():
    """正常路径：瓦解X使目标血限-10X%。"""
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    st.enemies.append(Entity("怪", "怪物", blood_limit=200, current_hp=200))
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    target = st.enemies[0]
    calc = DaoWenEngine.resolve("瓦解", 2, target=target, caster=st.player)
    r = c.apply_daowen_effect("瓦解", calc, st.player, target)
    assert target.blood_limit == 160, f"200-20%=160，实{target.blood_limit}"
    assert any(e["type"] == "wajie" for e in r["effects"])


def test_huaisi_is_the_only_heal_block():
    """正常路径：禁疗只剩【坏死】一个实现（2026-10-08 删【镇尸】）。

    坏死X：消耗2X。使[目标]无法获得回复，持续X。
    """
    DaoWenEngine.register_all()
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    st.enemies.append(Entity("怪", "怪物", blood_limit=200, current_hp=200))
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    target = st.enemies[0]
    assert "镇尸" not in DaoWenEngine._registry, "【镇尸】应已删除"
    calc = DaoWenEngine.resolve("坏死", 2, target=target, caster=st.player)
    r = c.apply_daowen_effect("坏死", calc, st.player, target)
    assert target.has_status("坏死")
    assert c._heal_blocked(target) is True


def test_gouhun_blocks_mana_gain_for_x_rounds():
    """正常路径（2026-10-08 用户令）：勾魂X挂到目标身上，持续X回合**无法获得法力**。

    旧版为「[回始]失去2X法力，持续∞」，已废止；现行版**不扣已有法力**，
    只压制一切法力增益（判定位：Entity.gain_mana()）。
    2026-09-09 曾因法力改一池制而改为「消耗翻倍」，2026-10-08 撤销。
    """
    st = GameState()
    # DM裁定 2026-09-09：轮回者普攻面板初始 1×1，且雕塑不再排除轮回者；
    # 夹具不给面板会在回终被雕塑化，勾魂的持续X就走不到期。
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                       mana_limit=20, current_mana=20, attack_count=1, attack_power=1)
    foe = Entity("敌法", "轮回者", blood_limit=60, current_hp=60,
                 mana_limit=20, current_mana=20, attack_count=1, attack_power=1)
    st.enemies.append(foe)
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    calc = DaoWenEngine.resolve("勾魂", 2, target=foe, caster=st.player)
    c.apply_daowen_effect("勾魂", calc, st.player, foe)
    assert foe.has_status("勾魂"), "勾魂应挂在目标身上"
    # 持续回合 = X = 2（不是永久）
    dur = next(s.remaining_rounds for s in foe.status_effects if s.name == "勾魂")
    assert dur == 2, f"勾魂X=2 应持续2回合，实{dur}"

    # 2026-10-08：勾魂 = 「无法获得[法力]」（消耗不翻倍）
    foe.current_mana = 20
    assert foe.can_gain_mana() is False, "勾魂期间不得获得法力"
    assert foe.gain_mana(9) == 0
    assert foe.current_mana == 20, f"增益应被压死，实{foe.current_mana}"
    assert foe.spend_mana(4) is True
    assert foe.current_mana == 16, f"勾魂不改消耗倍率，实剩 {foe.current_mana}"

    # 第2回合仍在持续期内（持续X=2，[回终]才递减）
    c.round_start({"relic_choices": {}})
    assert foe.has_status("勾魂")
    assert foe.can_gain_mana() is False, "持续期内仍无法获得法力"
    assert foe.spend_mana(2) is True
    assert foe.current_mana == 14, f"仍不翻倍，实剩 {foe.current_mana}"

    # 持续走完后恢复正常消耗
    from engine.enums import CombatSubphase
    st.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    c.round_end()
    st.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    c.round_end()
    assert not foe.has_status("勾魂"), "持续X走完后勾魂应自然到期"
    assert foe.can_gain_mana() is True, "到期后恢复获得法力的能力"
    before = foe.current_mana
    assert foe.gain_mana(3) == 3
    assert foe.current_mana == before + 3, "到期后增益恢复生效"
    assert foe.spend_mana(4) is True
