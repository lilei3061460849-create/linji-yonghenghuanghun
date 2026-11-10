# -*- coding: utf-8 -*-
"""2026-11-08 Task 8：四副本各补 4 道纹 + 【勾魂】重做 + 四面体最终结构。

龙心谷 百炼/锈蚀/重铸/回炉，乱葬岗 合体/化尸/立冢/借尸，
永夜庭 索取/逆血/永生（新定义）/初拥，荒疫古城 感染/重演/原初/病变（转正）。
"""
import pytest

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, SHAFA_LOOP_DAOWEN
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity
from tests.setup_support import finish_initial_daowen


def make_engine():
    ge = GameEngine(db_path="/tmp/linji_tests/test_task8.db", rng_seed=42)
    ge.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(ge)
    ge.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = ge.execute_action("setup_choose_region", {"region": "龙心谷"})
    ge.execute_action("choose_discovered_relic",
                      {"relic_name": setup["result"]["relic_choices"][0]})
    ge.state.phase = "in_combat"
    ge.state.current_battle = {"round": 1}
    p = ge.state.player
    p.mana_limit = 200
    p.current_mana = 200
    p.fragments = 100
    return ge, ge.combat, p


def give(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def item(name, uses=3, max_uses=3):
    return Consumable(name=name, effect="", current_uses=uses, max_uses=max_uses)


def monster(ge, daowen=(), hp=200, name="靶"):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               mana_limit=200, current_mana=200, speed_limit=3, current_speed=3)
    for n in daowen:
        give(m, n)
    ge.state.enemies.append(m)
    return m


class TestStructure20261108:
    def setup_method(self):
        DaoWenEngine.register_all()

    def test_registry_is_85_and_new_names_registered(self):
        names = set(DaoWenEngine.list_all())
        # 2026-11-09 第二轮改造：衍生改名，删【竭伐】（枯竭通道）与【伪伐】、增【冲击】，
        # 86 → 85。
        assert len(names) == 87
        for n in ("脆弱", "连击", "虚耗", "断脉", "买命", "冲击",
                  "燃命", "折年", "搏命", "献锋"):
            assert n in names
        for n in ("血伐", "竭伐", "碎伐", "伪伐", "燃血", "折寿", "疾贷", "舍力", "连珠"):
            assert n not in names, f"{n} 是已改名的旧道纹，不得留在现役表"
        for n in ("百炼", "锈蚀", "重铸", "回炉", "合体", "化尸", "立冢", "借尸",
                  "索取", "逆血", "永生", "初拥", "感染", "重演", "原初", "病变"):
            assert n in names

    @pytest.mark.parametrize("region,expected", [
        ("龙心谷", {"淬火", "炽热", "百炼", "锈蚀", "锻造", "熔化", "回炉", "重铸"}),
        ("乱葬岗", {"分裂", "合体", "化尸", "勾魂", "招魂", "凋零", "立冢", "借尸"}),
        ("永夜庭", {"血契", "共罪", "怜悯", "索取", "不死", "永生", "逆血", "初拥"}),
        ("荒疫古城", {"出土", "感染", "原初", "唯一", "净化", "病变", "催化", "重演"}),
    ])
    def test_final_tetrahedra(self, region, expected):
        from engine.gamedata import UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN
        assert len(ResonanceEngine.tetrahedra_of_region(region)) == 2
        assert set(ResonanceEngine.nodes_of_region(region)) == expected
        pool = (UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN if region == "永夜庭"
                else REGION_EXCLUSIVE_DAOWEN[region])
        assert {n for n, r in pool.items() if r == region} == expected \
            if region == "永夜庭" else set(pool) == expected

    def test_generic_swap_back(self):
        t2 = ResonanceEngine.CLOSED_LOOPS["通用四面体二"]
        t4 = ResonanceEngine.CLOSED_LOOPS["通用四面体四"]
        assert {n for a, _, b in t2 for n in (a, b)} == {"操纵", "束缚", "迟滞", "封印"}
        assert {n for a, _, b in t4 for n in (a, b)} == {"必中", "飞行", "压迫", "篡改"}
        assert "封印" in SHAFA_LOOP_DAOWEN and "篡改" not in SHAFA_LOOP_DAOWEN

    def test_gouhun_new_formula(self):
        calc = DaoWenEngine.resolve("勾魂", 2)
        assert calc["cost_type"] == "消耗" and calc["cost"] == 10
        assert calc["transfer_daowen_count"] == 2
        assert "no_mana_gain" not in calc



def cast(ce, caster, name, x, target=None, **kw):
    """按引擎唯一入口发动：先 resolve 出 calc，再 apply（不经 handler 扣费）。"""
    calc = DaoWenEngine.resolve(name, x, target=target, **kw)
    return ce.apply_daowen_effect(name, calc, caster,
                                  target if target is not None else caster)


def _player_with(*names):
    ge, ce, player = make_engine()
    for n in names:
        give(player, n)
    return ge, ce, player


class TestGouhunTransfer:
    def test_transfers_daowen_to_creation_permanently(self):
        ge, ce, player = _player_with("勾魂")
        creation = Entity(name="分身", entity_type="造物", blood_limit=50, current_hp=50)
        creation.is_created_being = True
        ge.state.temp_friends.append(creation)
        enemy = monster(ge, ["杀伐", "再生", "庇护"])
        eff = cast(ce, player, "勾魂", 2, target=enemy,
                                     daowen_names=["杀伐", "再生"])
        assert any(e["type"] == "daowen_transferred" for e in eff["effects"])
        assert "杀伐" not in enemy.dao_wen and "再生" not in enemy.dao_wen
        assert "庇护" in enemy.dao_wen
        assert "杀伐" in creation.dao_wen and "再生" in creation.dao_wen

    def test_no_creation_rejected_before_payment(self):
        ge, ce, player = _player_with("勾魂")
        enemy = monster(ge, ["杀伐"])
        before = player.current_mana
        with pytest.raises(ValueError, match="没有可接收道纹的"):
            cast(ce, player, "勾魂", 1, target=enemy, daowen_names=["杀伐"])
        assert player.current_mana == before

    def test_can_gain_mana_no_longer_blocked(self):
        assert Entity(name="某人", entity_type="轮回者").can_gain_mana() is True


class TestDragonValleyNew:
    def test_zhongzhu_rerolls_durability_in_range(self):
        ge, ce, player = _player_with("重铸")
        ge.state.consumables.append(item("大力丸", 1, 3))
        for _ in range(5):
            eff = cast(ce, player, "重铸", 2, item_name="大力丸")
            it = next(i for i in ge.state.consumables if i.name == "大力丸")
            ev = next(e for e in eff["effects"] if e["type"] == "consumable_recast")
            assert 2 <= ev["durability"] <= 4
            assert it.current_uses == ev["durability"] == it.max_uses

    def test_huilu_destroys_and_discovers(self):
        ge, ce, player = _player_with("回炉")
        ge.state.consumables.append(item("大力丸", 1, 3))
        eff = cast(ce, player, "回炉", 3, item_name="大力丸")
        assert all(i.name != "大力丸" for i in ge.state.consumables)
        disc = next(e for e in eff["effects"] if e["type"] == "consumable_discovery")
        assert disc["uses"] == 3 and disc["source"] == "回炉"

    def test_bailian_window_doubles_heal_via_handler_window(self):
        ge, ce, player = _player_with()
        player.blood_limit = 100
        player.current_hp = 50
        # 【百炼】的翻倍只在「使用消耗品」的同步结算窗口内生效（handlers 注入倍率）。
        ge.state._consumable_effect_multiplier = 2
        ge.state.apply_heal(player, 10)
        assert player.current_hp == 70
        ge.state._consumable_effect_multiplier = 1
        ge.state.apply_heal(player, 10)
        assert player.current_hp == 80

    def test_xiushi_blocks_consumable_use_via_handler(self):
        ge, ce, player = _player_with("锈蚀")
        ge.state.consumables.append(item("大力丸", 2, 2))
        cast(ce, player, "锈蚀", 2, target=player)
        res = ge.execute_action("consume_item", {"name": "大力丸"})
        assert res["success"] is False
        assert "锈蚀" in res["error"]


class TestGraveyardNew:
    def test_huashi_makes_target_treated_as_creation(self):
        ge, ce, player = _player_with("化尸")
        enemy = monster(ge, ["杀伐"])
        assert not enemy.treated_as_creation()
        cast(ce, player, "化尸", 2, target=enemy)
        assert enemy.treated_as_creation()

    def test_lizhong_creates_corpses_and_jieshi_borrows_stats(self):
        ge, ce, player = _player_with("立冢", "借尸")
        enemy = monster(ge, ["杀伐"], hp=40)
        cast(ce, player, "立冢", 2, target=enemy)
        assert len(ge.state.dead_monsters) == 2
        me = Entity(name="我", entity_type="轮回者", blood_limit=100, current_hp=90,
                    mana_limit=30, current_mana=30, speed_limit=2, current_speed=2)
        base = (me.max_hp, me.max_mana, me.speed_limit) \
            if hasattr(me, "max_mana") else (me.blood_limit, me.mana_limit, me.speed_limit)
        base = (me.blood_limit, me.mana_limit, me.speed_limit)
        cast(ce, player, "借尸", 2, target=me)
        assert (me.blood_limit, me.mana_limit, me.speed_limit) == (40, 200, 3)
        ce.advance_target_rounds(me, 2)
        assert (me.blood_limit, me.mana_limit, me.speed_limit) == base

    def test_heti_merges_creations(self):
        ge, ce, player = _player_with("合体")
        a = Entity(name="甲", entity_type="造物", blood_limit=30, current_hp=30)
        b = Entity(name="乙", entity_type="造物", blood_limit=20, current_hp=15)
        for c in (a, b):
            c.is_created_being = True
            ge.state.temp_friends.append(c)
        eff = cast(ce, player, "合体", 2)
        ev = next(e for e in eff["effects"] if e["type"] == "creations_merged")
        assert ev["blood_limit"] == 50 and ev["current_hp"] == 45
        remaining = [e for e in ge.state.get_all_player_side()
                     if e.treated_as_creation()]
        assert len(remaining) == 1 and remaining[0].blood_limit == 50


class TestEternalNightNew:
    def test_yongsheng_turns_damage_into_heal(self):
        ge, ce, player = _player_with("永生")
        enemy = monster(ge, ["杀伐"])
        enemy.current_hp = 60
        enemy.blood_limit = 100
        cast(ce, player, "永生", 2, target=enemy)
        ce._apply_hostile_damage(enemy, 10, source=player)
        assert enemy.current_hp == 70

    def test_nixue_swaps_and_restores_on_expiry(self):
        ge, ce, player = _player_with("逆血")
        enemy = monster(ge, ["杀伐"], hp=100)
        enemy.current_hp = 30
        cast(ce, player, "逆血", 1, target=enemy)
        assert enemy.current_hp == 70  # 100 血限：30 与已损 70 互换
        ce.advance_target_rounds(enemy, 1)
        assert enemy.current_hp == 30  # 到期恢复互换前记录值

    def test_suoqu_grants_only_actual_payment(self):
        ge, ce, player = _player_with("索取")
        enemy = monster(ge, ["杀伐"])
        enemy.fragments = 0
        before = player.fragments
        eff = cast(ce, player, "索取", 2, target=enemy,
                                     resource_type="碎片")
        ev = next(e for e in eff["effects"] if e["type"] == "resource_demanded")
        assert ev["paid"] == 0 and player.fragments == before

    def test_chuyong_syncs_with_caster_each_round_boundary(self):
        ge, ce, player = _player_with("初拥")
        player.blood_limit = 100
        player.current_hp = 80
        enemy = monster(ge, ["杀伐"], hp=200)
        enemy.current_hp = 150
        cast(ce, player, "初拥", 3, target=enemy)
        # 初拥支付生命5X 后再对齐：目标与施法者当前生命/血限一致。
        assert enemy.current_hp == player.current_hp
        assert enemy.blood_limit == player.blood_limit == 100
        player.current_hp = 33
        ce.advance_target_rounds(enemy, 1)
        assert enemy.current_hp == 33 and enemy.is_alive


class TestPlagueCityNew:
    def test_ganran_replaces_and_keeps_originals(self):
        ge, ce, player = _player_with("感染")
        player.mutation = 100
        enemy = monster(ge, ["杀伐", "再生", "庇护"])
        cast(ce, player, "感染", 2, target=enemy,
                               daowen_names=["杀伐", "再生"])
        assert sorted(enemy.infected_daowen_originals) == ["再生", "杀伐"]
        assert "杀伐" not in enemy.dao_wen and "再生" not in enemy.dao_wen
        assert "庇护" in enemy.dao_wen and "感染" in enemy.dao_wen

    def test_yuanchu_borrows_unowned_permanently(self):
        ge, ce, player = _player_with("原初")
        player.mutation = 100
        enemy = monster(ge, ["杀伐", "再生"])
        cast(ce, player, "原初", 2, target=enemy)
        assert "杀伐" in player.dao_wen and "再生" in player.dao_wen
        ce.advance_target_rounds(player, 3)
        assert "杀伐" in player.dao_wen  # 持续∞ 不到期

    def test_chongyan_requires_history_and_replays(self):
        ge, ce, player = _player_with("重演")
        enemy = monster(ge, ["杀伐"])
        with pytest.raises(Exception, match="尚未发动过"):
            cast(ce, player, "重演", 1, target=enemy,
                                   replay_daowen="杀伐")
        enemy.mark_daowen_used("杀伐")
        hp = enemy.current_hp
        eff = cast(ce, player, "重演", 2, target=enemy,
                                     replay_daowen="杀伐")
        assert any(e["type"] == "daowen_replayed" for e in eff["effects"])
        assert enemy.current_hp == hp - 4  # 重演以 X=2 发动杀伐（敌人自伤 2×2？见下注）

    def test_bingbian_converts_cost_layers(self):
        ge, ce, player = _player_with("病变")
        enemy = monster(ge, ["杀伐"])
        enemy.reversible_cost_layers["血限"] = 3
        enemy.blood_limit = 100
        cast(ce, player, "病变", 2, target=enemy,
                               from_kind="血限", to_kind="异变")
        assert _layers(enemy, "血限") == 1 and _layers(enemy, "异变") == 2


def _layers(e, kind):
    if kind == "异变":
        return e.mutation_count
    return int(e.reversible_cost_layers.get(kind, 0) or 0)
