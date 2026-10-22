"""
pytest 风格测试 - 里程碑8：修复5件坏掉/缺失的遗物（血誓戒/买路财/同魂笔/第一杯(原钱袋)/忘忧香）

历史背景：早期遗物池中只有7件真正在战斗中生效，血誓戒完全没做、买路财只有计算没有执行动作、同魂笔只生成
一条虚假日志不改任何状态、钱袋的触发点从未被调用过是死代码、忘忧香曾未注册。钱袋已删除；其当初并入【第一杯】的免疫癌变效果也已在 2026-09-23【第一杯】重做时废止。
2026-10-13：【买路财】改制为轮回者【逃跑】行动（一次付清5次出手强制脱战），本文件原 retreat_via_toll 四组用例已删除；逃跑回归见 tests/test_escape_and_diandang.py。

覆盖范围：
1. 血誓戒：[回始]玩家首次主动支付流血代价获得等量格挡/低血量时改为等量生命，每回合限一次
2. 买路财（已删除，见上）：原覆盖 retreat_via_toll 真正执行撤退(扣碎片/生命、清空战场)，不再只是算个数字
3. 同魂笔：第二目标的道纹也永久变为变化后的道纹，施法者同时永久获得
4. 第一杯（2026-09-23 用户令重做）：你受到的[回复]与失去的生命翻倍；
   旧「+50%回复、[回终]未回复流血10、免疫癌变」已废止，持有者照样癌变；朋友/员工不继承
5. 忘忧香：保持在遗物池中（2026-09-28 重做为战斗被动：溢出回复→等量格挡；局外【忘忧】行动已删除）

运行方式：
    python -m pytest tests/test_relic_fixes.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Relic, DaoWen, DaoWenInstance, Entity


def _new_engine(db_suffix: str, daowen="杀伐") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_relicfix_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    if daowen != "杀伐":
        _give_daowen(engine.state.player, daowen)
    engine.state.energy = 3
    return engine


def _give_daowen(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


def _start_with_enemy(engine, enemy):
    """battle_start会自动出怪，这里先start再替换为受控的测试怪物"""
    engine.state.energy = 0
    engine.execute_action("battle_start", {"relic_choices": {}})
    engine.state.enemies.clear()
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})


# ========================================================================
# 正常路径
# ========================================================================

def test_blood_oath_ring_grants_shield_on_first_bleed_cost():
    """血誓戒正常路径：本回合首次流血代价，获得等量格挡"""
    engine = _new_engine("oath_shield", daowen="连击")
    engine.state.relics.append(Relic(name="血誓戒", effect=""))
    _give_daowen(engine.state.player, "连击", cost_type="流血")
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))

    hp_before = engine.state.player.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "连击", "x": 5, "target": "靶怪"})
    assert r["success"] is True
    assert engine.state.player.shield == 25
    assert engine.state.player.current_hp == hp_before - 25


def test_blood_oath_ring_grants_life_instead_when_low_hp():
    """血誓戒正常路径：支付后生命≤30%时，改为获得等量生命而不是格挡"""
    engine = _new_engine("oath_life", daowen="连击")
    engine.state.relics.append(Relic(name="血誓戒", effect=""))
    _give_daowen(engine.state.player, "连击", cost_type="流血")
    engine.state.player.current_hp = 20  # blood_limit=60, 支付后20-15=5 <= 30%*60=18
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))

    r = engine.execute_action("use_daowen", {"daowen_name": "连击", "x": 3, "target": "靶怪"})  # 流血价5X=15
    assert r["success"] is True
    assert engine.state.player.shield == 0, "低血量时不应给格挡"
    assert engine.state.player.current_hp == 5 + 15, "应改为获得等量生命(15)"


def test_blood_oath_ring_only_once_per_round():
    """血誓戒正常路径：每回合只在首次流血代价时触发，第二次不再触发"""
    engine = _new_engine("oath_once", daowen="连击")
    engine.state.relics.append(Relic(name="血誓戒", effect=""))
    _give_daowen(engine.state.player, "连击", cost_type="流血")
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))

    engine.execute_action("use_daowen", {"daowen_name": "连击", "x": 3, "target": "靶怪"})
    shield_after_first = engine.state.player.shield
    engine.execute_action("use_daowen", {"daowen_name": "连击", "x": 4, "target": "靶怪"})
    assert engine.state.player.shield == shield_after_first, "同一回合第二次流血代价不应再给格挡"


# 2026-10-13：【买路财】改制为轮回者【逃跑】行动（一次付清5次出手强制脱战），
# 原 retreat_via_toll 四组用例删除；逃跑回归见 tests/test_escape_and_diandang.py。


def test_tonghunbi_grants_caster_real_daowen_from_second_target():
    """同魂笔正常路径：第二目标道纹永久变化，施法者同时永久获得变化后道纹"""
    engine = _new_engine("tongtonbi_ok")
    engine.state.relics.append(Relic(name="同魂笔", effect=""))
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "杀伐")
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪甲", entity_type="怪物", blood_limit=100, current_hp=100)
    _give_daowen(enemy, "固执", cost_type="冷却")
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转", "target_daowen": "再生",
        "second_target_ref": "enemy:0", "second_source_daowen": "固执",
    })
    assert r["success"] is True
    assert "连击" in engine.state.player.dao_wen, "施法者应永久获得固执反转后的连击"
    assert "固执" not in enemy.dao_wen and "连击" in enemy.dao_wen


def test_first_cup_doubles_heal_and_no_longer_blocks_cancer():
    """第一杯（2026-09-23 重做）：回复翻倍；旧「免疫癌变」条款废止，持有者照样癌变。

    旧断言（持有者 check_cancer 返回 None）编码的是已被用户令删除的规则，
    本用例按新规则重写；完整覆盖见 tests/test_first_cup_rework.py。
    """
    engine = _new_engine("firstcup_ok")
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    player = engine.state.player
    player.current_hp = 9

    detail = engine.state.apply_heal(player, 6)

    assert detail["heal_amount"] == 12, "持有第一杯应使受到的回复翻倍"
    assert player.current_hp == 21
    player.total_healed = engine.combat.cancer_threshold_of(player)
    hit = engine.combat.check_cancer(player)
    assert hit is not None
    assert not player.is_alive, "第一杯不再免疫癌变"


def test_first_cup_doubling_is_not_inherited_by_allies():
    """第一杯边界：朋友/员工不继承——既不被翻倍，也不会被免癌变。"""
    engine = _new_engine("firstcup_ally")
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    friend = Entity(name="同伴", entity_type="朋友", blood_limit=40, current_hp=10)
    engine.state.friends.append(friend)

    engine.state.apply_heal(friend, 5)

    assert friend.current_hp == 15, "非持有者回复量不变"
    friend.total_healed = engine.combat.cancer_threshold_of(friend)
    hit = engine.combat.check_cancer(friend)
    assert hit is not None
    assert not friend.is_alive and friend.is_proliferated


def test_wangyouxiang_registered_in_revised_relic_pool():
    """删除两件旧契约、钱袋与折速法印后，忘忧香仍在遗物池中（2026-10-03 新增 5 件 → 16；2026-10-13 删买路财/三相残韵盘 → 14）。"""
    engine = _new_engine("wangyou_registered")
    names = {n for n, _ in engine.RELIC_DEFS}
    assert "忘忧香" in names
    assert len(engine.RELIC_DEFS) == 26   # 万钧印已改为消耗品；忘忧香属于现行遗物池
    assert "血契" in names
    assert "钱袋" not in names


def test_wangyouxiang_out_of_battle_action_removed():
    """2026-09-28 用户令：忘忧香改为战斗被动（溢出回复→格挡），局外【忘忧】行动已删除。"""
    engine = _new_engine("wangyou_removed")
    engine.state.relics.append(Relic(name="忘忧香", effect=""))
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "忘忧", "tier": 2, "daowen_names": ["杀伐", "再生"],
    })
    assert r["success"] is False, "局外【忘忧】已删除，应返回失败"


def test_wangyouxiang_converts_overheal_to_block():
    """战斗被动：持有忘忧香时，溢出回复转化为等量格挡。"""
    engine = _new_engine("wangyou_passive")
    engine.state.relics.append(Relic(name="忘忧香", effect=""))
    _start_with_enemy(engine, Entity(name="大怪", entity_type="怪物", blood_limit=100, current_hp=100))
    p = engine.state.player
    p.current_hp = p.blood_limit  # 满血，任何回复都是溢出
    block_before = p.shield
    detail = engine.state.apply_heal(p, 12, ctx={
        "timing": "player_action", "source": "测试", "source_type": "test",
        "actor": p, "target": p, "mechanic": "heal", "subtype": "test", "amount": 12,
        "tags": {"test"}})
    assert detail["actual_heal"] == 0
    assert detail["overheal"] == 12
    assert detail.get("wangyouxiang_block") == 12
    assert p.shield == block_before + 12, f"溢出12应转12格挡，实得{p.shield - block_before}"


def test_wangyouxiang_does_nothing_without_relic():
    """无忘忧香时，溢出回复按原逻辑走（不转格挡）。"""
    engine = _new_engine("wangyou_no_relic")
    engine.state.relics = [r for r in engine.state.relics if r.name != "忘忧香"]
    _start_with_enemy(engine, Entity(name="大怪", entity_type="怪物", blood_limit=100, current_hp=100))
    p = engine.state.player
    p.current_hp = p.blood_limit
    block_before = p.shield
    detail = engine.state.apply_heal(p, 12, ctx={
        "timing": "player_action", "source": "测试", "source_type": "test",
        "actor": p, "target": p, "mechanic": "heal", "subtype": "test", "amount": 12,
        "tags": {"test"}})
    assert detail.get("wangyouxiang_block") is None
    assert p.shield == block_before


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_wangyouxiang_rejected_without_relic():
    """错误输入：没有忘忧香不能执行忘忧"""
    engine = _new_engine("wangyou_no_relic")
    _give_daowen(engine.state.player, "再生")
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action": "忘忧", "tier": 1, "daowen_names": ["再生"]})
    assert r["success"] is False
    assert "再生" in engine.state.player.dao_wen


def test_wangyouxiang_rejected_when_daowen_count_mismatches_tier():
    """错误输入：指定的道纹数量必须与档位一致，否则拒绝"""
    engine = _new_engine("wangyou_mismatch")
    engine.state.relics.append(Relic(name="忘忧香", effect=""))
    _give_daowen(engine.state.player, "再生")
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action": "忘忧", "tier": 2, "daowen_names": ["再生"]})
    assert r["success"] is False
    assert "再生" in engine.state.player.dao_wen, "拒绝的行动不应扣除道纹"


def test_moneybag_does_not_add_kill_shards():
    """错误对照：第一杯不改写击杀碎片，标准奖励仍按血限2%+道纹×5"""
    engine = _new_engine("moneybag_no_shard")
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="待宰怪", entity_type="怪物", blood_limit=100, current_hp=0)
    monster.is_alive = False
    engine.state.enemies.append(monster)
    r = engine.execute_action("battle_end", {})
    assert r["result"]["shard_reward"] == 2, "第一杯不得额外加碎片，100血限×2%=2"


def test_tonghunbi_rejected_when_second_target_lacks_daowen():
    """错误输入：第二目标不持有指定道纹时，不应产生任何状态变化"""
    engine = _new_engine("tongtonbi_missing")
    engine.state.relics.append(Relic(name="同魂笔", effect=""))
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "杀伐")
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪乙", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转", "target_daowen": "再生",
        "second_target_ref": "enemy:0", "second_source_daowen": "固执",
    })
    assert r["success"] is True  # 主残韵仍然成功，只是second分支未生效
    assert "未生效" in r["second_target_log"]
    assert "连击" not in engine.state.player.dao_wen


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
