import pytest
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
    """正常路径：3个幸存的乱葬岗专属道纹已注册并可解析。

    2026-10-03 删【尸爆】；2026-10-08 删【镇尸】（与【坏死】硬重复）。
    """
    DaoWenEngine.register_all()
    for name in ("分裂", "勾魂", "凋零", "招魂"):
        assert name in DaoWenEngine._registry, f"{name} 未注册"
        r = DaoWenEngine.resolve(name, 2, target=Entity("T", "怪物", blood_limit=100, current_hp=100),
                                 caster=Entity("C", "轮回者", blood_limit=60, current_hp=60))
        assert isinstance(r, dict) and r["dao_wen"] == name


def test_dungeon_resonance_tetrahedron_complete():
    """正常路径：乱葬岗两组四面体（各 4 节点 6 边）全部可达。

    2026-11-08 用户最终结构：四面体一=分裂/合体/化尸/勾魂，
    四面体二=招魂/凋零/立冢/借尸；任两节点恰好一条边，三种残韵各两条。
    """
    DaoWenEngine.register_all()
    expected = {
        "乱葬岗·四面体一": [
            ("分裂", "反转", "合体"), ("化尸", "反转", "勾魂"),
            ("分裂", "转换", "化尸"), ("合体", "转换", "勾魂"),
            ("分裂", "曲解", "勾魂"), ("合体", "曲解", "化尸"),
        ],
        "乱葬岗·四面体二": [
            ("招魂", "反转", "凋零"), ("立冢", "反转", "借尸"),
            ("招魂", "转换", "立冢"), ("凋零", "转换", "借尸"),
            ("招魂", "曲解", "借尸"), ("凋零", "曲解", "立冢"),
        ],
    }
    for loop_name, expected_edges in expected.items():
        edges = ResonanceEngine.CLOSED_LOOPS[loop_name]
        assert len(edges) == 6
        for edge in expected_edges:
            assert edge in edges, f"{loop_name}缺少边 {edge}"
        nodes = {n for src, _, dst in edges for n in (src, dst)}
        assert all(name in DaoWenEngine._registry for name in nodes)
        assert all(slot not in nodes for slot in RELICIZED_DAOWEN_SLOTS.values())


def test_reserved_slot_not_obtainable():
    """边界：闭环上的空占位符查得到，但**不能获得**——残韵走到它=无事发生。

    不消耗残韵、不改写目标牌面、不授予施法者（2026-10-08 用户令）。
    """
    DaoWenEngine.register_all()
    stock = {"转换": 3, "曲解": 3}
    # 2026-11-02：占位符整批撤销，任何旧占位名都不再是残韵结果。
    r = ResonanceEngine.apply_resonance(
        "分裂", "转换", True, True, dict(stock),
        target_daowen=RELICIZED_DAOWEN_SLOTS["缄默"])
    assert r["success"] is False
    assert stock["转换"] == 3, "非法目标不得消耗残韵"
    # 真实边照常成立：勾魂 --转换--> 合体（2026-11-08 四面体一：转换 分裂⇄化尸、合体⇄勾魂）。
    r2 = ResonanceEngine.apply_resonance("勾魂", "转换", True, True, dict(stock),
                                         target_daowen="合体")
    assert r2["success"] is True and r2["target"] == "合体"
    assert RESERVED_SLOT not in DaoWenEngine._registry
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, MONSTER_TRANSFORM_DAOWEN
    assert RESERVED_SLOT not in REGION_EXCLUSIVE_DAOWEN.get("乱葬岗", set())
    assert RESERVED_SLOT not in MONSTER_TRANSFORM_DAOWEN
    assert all(RESERVED_SLOT not in (src, dst)
               for loop in ("乱葬岗·四面体一", "乱葬岗·四面体二")
               for src, _, dst in ResonanceEngine.CLOSED_LOOPS[loop])


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
    """旧附煞菜单已删除；造生/妖怪承接其设计位置，不再给普通道纹附加煞气。"""
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

def test_wajie_is_removed_instead_of_reducing_blood_limit():
    with pytest.raises(ValueError, match="未知道纹"):
        DaoWenEngine.resolve("瓦解", 2)


def test_huaisi_is_the_only_heal_block():
    """正常路径：禁疗只剩【坏死】一个实现（2026-10-08 删【镇尸】）。

    坏死X：消耗5X。使[目标]无法获得回复，持续X。
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


def test_gouhun_transfers_daowen_to_creation():
    """正常路径（2026-11-08 用户令重做）：勾魂X=法力5X，把[目标]X种道纹
    转移至1个[造物]身上，持续∞。旧「无法获得[法力]」整条撤销：
    不挂状态、不压制法力增益。"""
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                       mana_limit=20, current_mana=20, attack_count=1, attack_power=1)
    foe = Entity("敌法", "轮回者", blood_limit=60, current_hp=60,
                 mana_limit=20, current_mana=20, attack_count=1, attack_power=1)
    from engine.models import DaoWen, DaoWenInstance
    for dw in ("杀伐", "再生"):
        foe.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    st.enemies.append(foe)
    creation = Entity("尸傀", "造物", blood_limit=30, current_hp=30)
    st.temp_friends.append(creation)
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    calc = DaoWenEngine.resolve("勾魂", 1, target=foe, caster=st.player,
                                daowen_names=["杀伐"])
    assert calc["cost"] == 5  # 法力5X
    c.apply_daowen_effect("勾魂", calc, st.player, foe)
    assert "杀伐" not in foe.dao_wen and "再生" in foe.dao_wen
    assert "杀伐" in creation.dao_wen
    assert not foe.has_status("勾魂"), "新勾魂不挂持续状态"
    foe.current_mana = 10
    assert foe.can_gain_mana() is True
    assert foe.gain_mana(5) == 5 and foe.current_mana == 15
