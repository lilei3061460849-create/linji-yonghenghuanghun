"""
引擎单元测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/linji_tests", exist_ok=True)

from engine.api import GameEngine
from engine.models import Entity, StatusEffect
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.dice import DiceEngine
from engine.enums import CostType, EntityType
from engine.gamedata import SHAFA_LOOP_DAOWEN
from tests.monster_phase_support import resolve_monster_phase
from tests.setup_support import finish_initial_daowen, grant_extra_actions
import math


def _choose_region(engine, region):
    if sum(engine.state.resonance.values()) == 0:
        engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    result = engine.execute_action("setup_choose_region", {"region": region})
    if result.get("success") and engine.state.pending_relic_choices:
        # 通用流程测试不覆盖需要额外显式选择的遗物；相关遗物有独立测试。
        optional = {"回锋刀", "共罪"}   # 2026-10-13：【三相残韵盘】删除
        choice = next((n for n in engine.state.pending_relic_choices if n not in optional),
                      engine.state.pending_relic_choices[0])
        engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    return result


def test_setup():
    """测试开局流程：属性分配后先发现遗物3选1，再从杀伐闭环发现初始道纹3选1。"""
    print("\n=== 测试：开局 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db", rng_seed=7)

    result = engine.execute_action("setup_attributes", {
        "name": "测试轮回者",
        "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    assert result["success"], f"属性分配失败: {result}"
    # 1属性点=6[血限]、2属性点=1[速限]=1[法限]；所有角色基础出手 1 次（单行动契约）
    assert engine.state.player.blood_limit == 66, f"血限错误: {engine.state.player.blood_limit}"
    assert engine.state.player.speed_limit == 4, f"速限错误: {engine.state.player.speed_limit}"
    assert engine.state.player.mana_limit == 3, f"法限错误: {engine.state.player.mana_limit}"
    assert engine.state.player.action_count == 1, "出手次数错误：基础出手应为 1 次"
    assert engine.state.shards == 20, "初始碎片错误"
    relic_choices = list(engine.state.pending_relic_choices)
    assert result["result"]["relic_choices"] == relic_choices
    assert len(relic_choices) == 3 and len(set(relic_choices)) == 3
    assert engine.state.pending_initial_daowen_choices == []
    assert engine.state.player.dao_wen == {}
    print("  ✓ 属性分配正确，先列出3件遗物发现候选")

    blocked = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "杀伐"})
    assert not blocked["success"], "遗物未选择时不能选初始道纹"
    print("  ✓ 未选择开局遗物时不能选初始道纹")

    picked_relic = engine.execute_action("choose_discovered_relic",
                                         {"relic_name": relic_choices[0]})
    assert picked_relic["success"], f"遗物选择失败: {picked_relic}"
    assert engine.state.relics and engine.state.relics[0].name == relic_choices[0]
    choices = list(engine.state.pending_initial_daowen_choices)
    assert picked_relic["result"]["daowen_choices"] == choices
    assert len(choices) == 3 and len(set(choices)) == 3
    assert set(choices) <= set(SHAFA_LOOP_DAOWEN)
    assert engine.state.player.dao_wen == {}
    print(f"  ✓ 显式选择开局遗物【{relic_choices[0]}】后，列出3个杀伐闭环发现候选")

    blocked = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert not blocked["success"] and "初始道纹" in blocked["error"]
    print("  ✓ 未选择初始道纹时不能选残韵")

    result = engine.execute_action("setup_attributes", {
        "blood_points": 5, "speed_points": 5, "mana_points": 5
    })
    assert not result["success"], "应该拒绝错误的属性分配"
    print("  ✓ 错误分配被拒绝")

    illegal = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "狂暴"})
    assert not illegal["success"]
    assert engine.state.player.dao_wen == {}
    print("  ✓ 非发现候选被拒绝")

    picked = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": choices[0]})
    assert picked["success"]
    assert set(engine.state.player.dao_wen) == {choices[0]}
    repeat = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": choices[0]})
    assert not repeat["success"]
    removed = engine.execute_action("setup_choose_daowen", {"daowen": "波及"})
    assert not removed["success"]
    print(f"  ✓ 显式选择初始道纹【{choices[0]}】，旧接口不能改写")

    result = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert result["success"], f"残韵选择失败: {result}"
    assert engine.state.resonance.get("反转", 0) == 1, "残韵计数错误"
    print("  ✓ 残韵选择正确")

    result = _choose_region(engine, "扭曲都市")
    assert result["success"], f"副本选择失败: {result}"
    assert engine.state.current_region == "扭曲都市", "副本设置错误"
    assert engine.state.phase == "pre_battle", "阶段切换错误"
    print("  ✓ 副本选择正确，进入局外阶段")


def test_daowen_calculations():
    """测试道纹计算"""
    print("\n=== 测试：道纹计算 ===")
    
    # 测试杀伐
    target = Entity(name="目标", entity_type=EntityType.MONSTER.value, blood_limit=100, current_hp=100)
    result = DaoWenEngine.resolve("杀伐", 3, target=target)
    assert result["cost"] == 3, f"杀伐消耗错误: {result['cost']}"
    assert result["target_life_loss"] == 9, f"杀伐伤害错误: {result['target_life_loss']}"   # X²=3²
    print("  ✓ 杀伐X=3: 消耗3，伤害15（DM裁定 2026-09-10：5X）")
    
    # 测试庇护
    result = DaoWenEngine.resolve("庇护", 5, target=target)
    assert result["cost"] == 5
    assert result["shelter_charges"] == 5 and result["duration"] == 5
    print("  ✓ 庇护X=5: 消耗5，免疫非[目标]来源的下5次失去生命，持续5")
    
    # 测试再生
    result = DaoWenEngine.resolve("再生", 4, target=target)
    assert result["cost"] == 4
    assert result["target_heal"] == 16
    print("  ✓ 再生X=4: 消耗4，回复16")
    
    # 测试波及
    result = DaoWenEngine.resolve("波及", 2)
    assert result["cost"] == 4          # 2026-09-16：消耗3X→2X
    assert result["mark_targets"] == 2
    assert result["duration"] == -1
    print("  ✓ 波及X=2: 消耗4，选择2个目标建立/解除波及（持续∞）")
    
    # 测试封印（代价：异变X；一个目标怪物延后X回合回场）
    result = DaoWenEngine.resolve("封印", 1)
    assert result["cost_type"] == CostType.MUTATION.value
    assert result["cost_mutation"] == 1
    assert result["delay_rounds"] == 1
    print("  ✓ 封印X=1: 异变+1，一个目标怪物延后1回合回场")
    
    
    print("  ✓ 所有道纹计算通过")


def test_resonance():
    """测试残韵系统"""
    print("\n=== 测试：残韵系统 ===")
    
    # 杀伐 → 反转 → 再生（杀伐的反转通向再生与封印两个相邻节点，须显式指定走向）
    result = ResonanceEngine.apply_resonance("杀伐", "反转", False, True, target_daowen="再生")
    assert result["success"], f"残韵失败: {result}"
    assert result["target"] == "再生"
    print("  ✓ 杀伐 --反转--> 再生")

    # 路径双向：再生 --反转--> 杀伐 同样成立（2026-10-07 用户令）
    result = ResonanceEngine.apply_resonance("再生", "反转", False, True, target_daowen="杀伐")
    assert result["success"], f"残韵失败: {result}"
    assert result["target"] == "杀伐"
    print("  ✓ 再生 --反转--> 杀伐（反向）")
    
    # 再生 → 曲解 → 庇护
    result = ResonanceEngine.apply_resonance("再生", "曲解", False, True, target_daowen="庇护")
    assert result["success"]
    assert result["target"] == "庇护"
    print("  ✓ 再生 --曲解--> 庇护")
    
    # 全力 → 反转 → 弱化（现役唯一出边，无需额外指定目标）
    result = ResonanceEngine.apply_resonance("全力", "反转", False, True)
    assert result["success"]
    assert result["target"] == "弱化"
    print("  ✓ 全力 --反转--> 弱化")
    
    # 查看可用残韵
    available = ResonanceEngine.get_available_resonance("杀伐")
    assert len(available) > 0
    print(f"  ✓ 杀伐可用残韵: {[a['resonance_type'] + '→' + a['target_daowen'] for a in available]}")
    
    # 不存在的路径
    result = ResonanceEngine.apply_resonance("杀伐", "转换", False, True)
    assert not result["success"]
    print("  ✓ 不存在的路径被正确拒绝")


def test_combat():
    """测试战斗系统"""
    print("\n=== 测试：战斗系统 ===")
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_rulings_{os.getpid()}.db")
    
    # 设置玩家
    engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine)
    
    # 添加怪物
    monster = Entity(
        name="测试怪物",
        entity_type=EntityType.MONSTER.value,
        blood_limit=80,
        current_hp=80,
        attack_count=3,
        attack_power=5,
    )
    engine.state.enemies.append(monster)
    
    # 测试伤害计算
    damage_result = monster.take_damage(20, "普通")
    assert damage_result["actual_damage"] == 20
    assert monster.current_hp == 60
    print(f"  ✓ 伤害20: HP 80→60")
    
    # 测试格挡
    monster.gain_shield(10)
    damage_result = monster.take_damage(15, "普通")
    assert damage_result["shield_absorbed"] == 10
    assert damage_result["actual_damage"] == 5
    print(f"  ✓ 格挡10吸收10伤害: 15伤害→5实际")
    
    # 测试回复
    monster.current_hp = 40
    heal_result = monster.heal(20)
    assert heal_result["actual_heal"] == 20
    assert monster.current_hp == 60
    print(f"  ✓ 回复20: HP 40→60")
    
    # 测试过量回复
    monster.current_hp = 75
    heal_result = monster.heal(20)
    assert heal_result["overheal"] == 15, f"过量应为15，实际{heal_result['overheal']}"
    assert monster.current_hp == 80
    print(f"  ✓ 过量回复: HP 75→80, 过量15")
    
    # 测试死亡
    monster.current_hp = 5
    damage_result = monster.take_damage(10)
    assert damage_result["died"] == True
    assert not monster.is_alive
    print(f"  ✓ 致死伤害: HP 5→0, 死亡")
    
    # 测试状态效果
    entity = Entity(name="测试", entity_type="轮回者", blood_limit=100, current_hp=100)
    entity.add_status(StatusEffect(name="兴奋", remaining_rounds=3, value=2))
    assert entity.has_status("兴奋")
    assert entity.get_status_value("兴奋") == 2
    print(f"  ✓ 状态效果添加: 兴奋3回合，值2")
    
    # 测试合并
    entity.add_status(StatusEffect(name="兴奋", remaining_rounds=2, value=1))
    assert entity.get_status_value("兴奋") == 3
    print(f"  ✓ 同名状态合并: 兴奋值3")
    
    # 测试递减
    expired = entity.tick_status_effects()
    assert not entity.has_status("兴奋") or entity.get_status_value("兴奋") == 3
    print(f"  ✓ 回合递减")
    
    print("  ✓ 战斗系统测试通过")


def test_dice():
    """测试随机数系统"""
    print("\n=== 测试：随机数系统 ===")
    dice = DiceEngine()
    
    # 创建池
    options = ["事件A", "事件B", "事件C", "事件D", "事件E"]
    result = dice.create_pool("test_pool", options)
    assert result["count"] == 5
    assert result["range"] == "1~5"
    print(f"  ✓ 创建池: {result['count']}个选项，范围{result['range']}")
    
    # 解析
    result = dice.resolve_pool("test_pool", 3)
    assert result["selected"] == "事件C"
    print(f"  ✓ 选择3: {result['selected']}")
    
    # 池缩小
    status = dice.get_pool_status("test_pool")
    assert status["count"] == 4
    print(f"  ✓ 池缩小: {status['count']}个剩余")
    
    # 超范围
    try:
        dice.resolve_pool("test_pool", 10)
        assert False, "应该抛出错误"
    except ValueError:
        print("  ✓ 超范围数字被拒绝")
    
    print("  ✓ 随机数系统测试通过")


def test_dm_rulings():
    """测试DM裁定系统"""
    print("\n=== 测试：DM裁定系统 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    
    # 设置
    engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    engine.state.phase = "in_combat"
    
    # 添加怪物
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=50, current_hp=10, 
                     attack_count=2, attack_power=5)
    engine.state.enemies.append(monster)
    
    # 声明许愿（轮回者向"某人"祈求）
    result = engine.execute_action("declare_wish", {"wish_text": "让这只怪物消失", "target_ref": "enemy:0"})
    assert result["success"]
    assert result["interrupt"]["interrupt_type"] == "许愿"
    print("  ✓ 许愿中断触发")
    
    # DM裁定（"某人"以扭曲方式实现愿望，代价不公开）
    result = engine.submit_ruling(
        "许愿",
        "愿望实现：怪物消失，但轮回者失去全部法力并本场无法恢复",
        {}
    )
    assert result["success"]
    assert result["ruling_id"] > 0
    print(f"  ✓ DM裁定保存，ID={result['ruling_id']}")
    
    # 查询先例
    precedent = engine.check_precedent("许愿", {"target_ref": "enemy:0"})
    assert precedent["found"]
    print(f"  ✓ 查询到{precedent['count']}个先例")
    
    print("  ✓ DM裁定系统测试通过")


def test_full_flow():
    """测试完整流程"""
    print("\n=== 测试：完整流程 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    
    # 开局
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _choose_region(engine, "扭曲都市")
    print("  ✓ 开局完成")
    
    # 局外行动（【领悟】已于 2026-09-10 删除，残韵不再能凭精力白拿）
    gone = engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
    assert gone["success"] is False and "未知局外行动" in gone["error"], gone
    print("  ✓ 领悟已删除：调用被拒且精力不泄漏")

    speed_before = engine.state.player.speed_limit
    # 2属性点=1速限：tier1 只给1点、买不到任何面板（余点入池），要用 tier2 才买得到
    engine.state.shards = max(engine.state.shards, 15)
    result = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 2, "to": "speed"})
    assert result["success"], f"修行失败: {result}"
    assert engine.state.player.speed_limit == speed_before + 1, "修行应+1速限"
    print(f"  ✓ 修行：速限{speed_before}→{engine.state.player.speed_limit}")
    
    heal_amt = math.ceil(engine.state.player.blood_limit * 0.2)
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}],
    })
    assert result["success"]
    print(f"  ✓ 休整：{heal_amt}点恢复量")
    
    # 开局3点精力：修行1 + 休整1 = 2。【领悟】已于 2026-09-10 删除、被拒不扣精力，
    # 故此处余 1；进战斗要求精力耗尽，用【探索】把最后一点花掉。
    assert engine.state.energy == 1, f"精力应为1（修行+休整各1，领悟已删不再扣），实际{engine.state.energy}"
    # 用【休整】而非【探索】收尾：探索会随机触发事件（本种子抽到【血肉温室】），
    # 未结算的事件会挡住后续所有行动。休整不触发事件，可安全重复。
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0",
                              "amount": math.ceil(engine.state.player.blood_limit * 0.2)}],
    })
    assert result["success"], result
    assert engine.state.energy == 0, f"精力应耗尽，实际{engine.state.energy}"
    print("  ✓ 精力耗尽")
    
    # 进入战斗
    result = engine.execute_action("battle_start", {})
    assert result["success"], result
    assert engine.state.current_battle == 1
    print(f"  ✓ 进入第1场战斗")
    
    # 回始；若随机发现的是回锋刀，则显式选择一个合法敌方目标。
    round_relic_choices = {}
    if any(relic.name == "回锋刀" for relic in engine.state.relics):
        round_relic_choices["回锋刀"] = {"enemy_index": 0}
    if any(relic.name == "共罪" for relic in engine.state.relics):
        round_relic_choices["共罪"] = {"use": False}
    result = engine.execute_action("round_start", {"relic_choices": round_relic_choices})
    assert result["success"], result
    # 回始获得等同当前法限的法力；战始已清零，无遗物时恰好等于法限。
    assert engine.state.player.current_mana >= engine.state.player.mana_limit
    print(f"  ✓ 回始：法力不低于法限（当前{engine.state.player.current_mana}/{engine.state.player.mana_limit}）")
    
    # 使用道纹
    monster = Entity(name="千手蜈蚣", entity_type="怪物", blood_limit=120, current_hp=120,
                     attack_count=6, attack_power=8)
    engine.state.enemies.append(monster)
    
    engine.state.player.actions_used_this_round = 0   # 轮回者每回合固定2次，前序行动已用满
    engine.state.player.current_mana = 20   # 新口径法限=加点//2 只有3，杀伐X=5 需5法力
    result = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐",
        "x": 5,
        "target_ref": f"enemy:{len(engine.state.enemies) - 1}",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"], result
    print(f"  ✓ 发动杀伐X=5: 对千手蜈蚣造成25伤害（5X）")
    
    print("  ✓ 完整流程测试通过")


def test_sculpture_and_proliferation():
    """测试雕塑（攻击力归0）与癌变（恢复达阈值）路径"""
    print("\n=== 测试：雕塑 / 癌变 胜利路径 ===")
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    from engine.models import GameState

    # --- 雕塑：把攻击力打到0 ---
    state = GameState()
    # DM裁定 2026-09-09：轮回者有普攻面板（初始 1×1），且雕塑不再排除轮回者——
    # 夹具必须给出合法面板，否则本条要测的「怪物雕塑」会被玩家自己的雕塑干扰。
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_count/attack_power
    # 面板无效——必须给当前速度/当前法力，否则玩家自己先满足雕塑条件、干扰本条断言。
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                    speed_limit=1, current_speed=1, mana_limit=1, current_mana=1)
    state.player = player
    m = Entity(name="石像鬼", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=2, attack_power=10)
    state.enemies.append(m)
    combat = CombatEngine(state, DiceEngine())
    m.attack_power = 0  # 模拟被弱化/僵化到0
    m.attack_count = 0  # DM裁定 2026-09-10：雕塑触发条件为攻次与攻力**都**为0
    paths = combat.settle_victory_paths()
    assert any(p["type"] == "sculpture" for p in paths), "应触发雕塑"
    assert m.is_sculptured and not m.is_alive
    sc = [c for c in state.consumables if c.kind == "sculpture"][0]
    assert sc.name == "石像鬼雕塑"
    assert sc.current_uses == 5  # 100血限×5%=5
    print(f"  ✓ 攻击力归0→触发雕塑，生成【{sc.name}】（{sc.current_uses}/{sc.max_uses}）")
    # 使用雕塑造伤
    target = Entity(name="靶怪", entity_type="怪物", blood_limit=50, current_hp=50)
    state.enemies.append(target)
    r = combat.use_sculpture(sc, target=target, mode="damage")
    assert r["success"] and r["damage"] == 15
    assert target.current_hp == 35
    print(f"  ✓ 雕塑赋能：对靶怪造成15伤害，剩余耐久{sc.current_uses}")

    # --- 癌变：恢复量达血限阈值 ---
    state2 = GameState()
    p2 = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state2.player = p2
    m2 = Entity(name="肉瘤", entity_type="怪物", blood_limit=80, current_hp=40,
                attack_count=1, attack_power=5)
    state2.enemies.append(m2)
    combat2 = CombatEngine(state2, DiceEngine())
    # 对怪物过量恢复：实恢40 + 过量160按原值计 → total_healed=200 ≥ 80（双倍机制已删）
    m2.heal(200)
    assert m2.total_healed >= 80
    paths2 = combat2.settle_victory_paths()
    assert any(p["type"] == "proliferation" for p in paths2), "应触发癌变"
    assert m2.is_proliferated and not m2.is_alive
    # 2026-10-05 用户令：癌变不再加成休整，奖励统一为【无限肉块】（1/1）
    meats = [c for c in state2.consumables if c.kind == "infinite_meat"]
    assert len(meats) == 1 and meats[0].name == "无限肉块"
    assert meats[0].current_uses == 1 and meats[0].max_uses == 1
    assert "恢复10生命" in meats[0].effect
    print("  ✓ 恢复量超阈值→触发癌变，吸收进死者之书（获得【无限肉块】）")
    print("  ✓ 雕塑/癌变路径测试通过")


def test_daowen_effects_wired():
    """测试道纹效果真实落地（攻面板/速度/碎片/状态/变形）"""
    print("\n=== 测试：道纹效果落地 ===")
    from engine.models import StatusEffect
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    # 本测试只验证道纹面板效果；开局随机发现的【衰变沙漏】会把所有道纹本次X减1，需从夹具中移除以隔离断言。
    engine.state.relics = [relic for relic in engine.state.relics if relic.name != "衰变沙漏"]
    engine.state.phase = "in_combat"
    player = engine.state.player
    # 本测试连续发动道纹只为验证效果落地，与出手预算校验无关；
    # 出手数不与速限换算，显式清理旧档计数并给予独立测试预算。
    player.actions_used_this_round = 0
    grant_extra_actions(player, 7)
    # 给玩家多个道纹用于测试
    from engine.models import DaoWen, DaoWenInstance
    for n in ["弱化","全力","变形","烁金"]:
        player.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100, attack_count=3, attack_power=10)
    m.shards = 20
    engine.state.enemies.append(m)
    player.current_mana = 99

    # 弱化3 → 攻击力＝当前法力-3（状态层修正，单行动契约下每次发动各占一个回合的出手）
    engine.state.player.actions_used_this_round = 0
    engine.state.duel_turn = "player_side"
    r = engine.execute_action("use_daowen", {"daowen_name":"弱化","x":3,"target":"靶怪"})
    assert r["success"], r
    assert m.get_status_value("弱化") == 3, f"弱化层数应3，实{m.get_status_value('弱化')}"
    assert m.effective_attack_power() == max(0, m.current_mana - 3), \
        f"弱化后攻击力应＝当前法力{m.current_mana}-3，实{m.effective_attack_power()}"
    # 全力2 → 攻击力**锁定为其[法限]**，持续X。
    # 旧版「攻击力+X，持续∞」写遗留字段，属性统一后对不写穿的轮回者无效，已废止。
    engine.state.player.actions_used_this_round = 0
    engine.state.duel_turn = "player_side"
    r = engine.execute_action("use_daowen", {"daowen_name":"全力","x":2,"target":"靶怪"})
    assert r["success"], r
    assert m.effective_attack_power() == m.mana_limit, \
        f"强化后攻击力应锁定为法限{m.mana_limit}，实{m.effective_attack_power()}"
    mana_before = m.current_mana
    m.current_mana = 1      # 花掉法力
    assert m.effective_attack_power() == m.mana_limit, "强化期间花法力不应掉攻击力"
    m.current_mana = mana_before
    print(f"  ✓ 弱化/强化：靶怪攻击力 10→7→锁定法限{m.mana_limit}（花法力不掉）")

    # R35 烁金2：目标碎片减少20，施法者不获得。
    shards_before = engine.state.shards
    # 前序弱化/全力各占一个回合的出手：起新回合并把出手权归本侧
    engine.state.player.actions_used_this_round = 0
    engine.state.duel_turn = "player_side"
    r = engine.execute_action("use_daowen", {"daowen_name":"烁金","x":2,"target":"靶怪"})
    assert r["success"], f"烁金失败: {r}"
    assert m.shards == 0, f"烁金X2应烧掉靶怪现有20碎片，实{m.shards}"
    assert engine.state.shards == shards_before, "烁金销毁碎片，玩家不获得"
    print(f"  ✓ 烁金：靶怪碎片20→0，玩家碎片不变")

    # 变形（自身攻击力/攻击次数互换）：玩家默认 0×0，互换后仍为 0×0，只验证逻辑跑通
    engine.state.player.actions_used_this_round = 0
    engine.state.duel_turn = "player_side"
    r = engine.execute_action("use_daowen", {"daowen_name":"变形","x":1})
    assert r["success"], r
    print("  ✓ 变形：攻击力/攻击次数互换执行成功")
    print("  ✓ 道纹效果落地测试通过")


def test_out_of_combat_actions():
    """测试局外行动真实生效（休整回血/学习加道纹法术/共鸣给遗物）"""
    print("\n=== 测试：局外行动落地 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "罪孽都市")
    player = engine.state.player

    # 休整：先扣血再休整，验证回血
    player.current_hp = 20
    heal_amt2 = math.ceil(player.blood_limit * 0.4)
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 2,
        "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt2}],
    })
    assert r["success"], f"休整失败: {r}"
    expected_hp = min(player.blood_limit, 20 + heal_amt2)
    assert player.current_hp == expected_hp, f"休整2档应回{heal_amt2}→{expected_hp}，实{player.current_hp}"
    print(f"  ✓ 休整2档：HP20→{player.current_hp}")

    # 学习道纹：若开局已发现庇护则改学波及
    learn_name = "庇护" if "庇护" not in player.dao_wen else "波及"
    r = engine.execute_action("pre_battle_action", {"sub_action":"学习","sub":"daowen","name": learn_name})
    assert r["success"], f"学习失败: {r}"
    assert learn_name in player.dao_wen, f"{learn_name}应已加入玩家道纹"
    print(f"  ✓ 学习道纹：玩家道纹={list(player.dao_wen.keys())}")

    # 法术统一通过战斗中的自定义施法定义，立即写入 spells 并消耗一次主动出手。
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    grant_extra_actions(player, 7)  # 阶数＝重数后，2 阶法术要 2 次出手（基础 1 出手，先补足）
    used = player.actions_used_this_round
    r = engine.execute_action("define_spell", {"spell_name": "先发制人"})
    assert r["success"], f"定义法术失败: {r}"
    assert "先发制人" in [spell.name for spell in player.spells]
    assert player.armed_spells == []
    assert player.actions_used_this_round == used + 1, "定义法术固定消耗1次出手（阶级不预付出手）"
    r = engine.execute_action("undefine_spell", {"spell_name": "先发制人"})
    assert r["success"], f"移除法术失败: {r}"
    assert player.spells == []
    assert player.actions_used_this_round == used + 1, "移除不应再扣出手"
    engine.state.phase = "pre_battle"
    print("  ✓ 法术定义/移除：持道纹即可定义，定义耗1次出手")

    # 共鸣：获得遗物（补满精力以便测试）
    engine.state.energy = 3
    n_before = len(engine.state.relics)
    r = engine.execute_action("pre_battle_action", {"sub_action":"共鸣","sub":"discover"})
    assert r["success"], f"共鸣失败: {r}"
    picked = engine.execute_action("choose_discovered_relic", {
        "relic_name": r["result"]["relic_choices"][0],
    })
    assert picked["success"] and len(engine.state.relics) == n_before + 1
    print(f"  ✓ 共鸣：显式选择遗物【{picked['result']['relic']}】，持有{len(engine.state.relics)}件")
    print("  ✓ 局外行动落地测试通过")


def test_relic_effects():
    """测试遗物效果触发（避风铃/第一杯/回锋刀）"""
    print("\n=== 测试：遗物效果 ===")
    from engine.models import Relic, GameState
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    # 避风铃：闪避+3挡
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    st.relics = [Relic(name="避风铃", effect="")]
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=10)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine())
    r = combat.resolve_attack(m, st.player, dodge=True)
    assert r["dodge_success"] and st.player.shield == 3, f"避风铃应+3挡，实{st.player.shield}"
    print("  ✓ 避风铃：闪避后+3格挡")

    # 回锋刀：回始对敌造伤(3×失速)
    st2 = GameState(); st2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=5)
    st2.relics = [Relic(name="回锋刀", effect="")]
    m2 = Entity(name="靶", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=1)
    st2.enemies.append(m2)
    combat2 = CombatEngine(st2, DiceEngine())
    combat2.round_start({"回锋刀": {"enemy_index": 0}})  # 显式选择回锋刀目标
    assert m2.current_hp < 120, f"回锋刀应造伤，实HP{m2.current_hp}"
    print(f"  ✓ 回锋刀：回始造伤(失速3→9伤)，靶HP120→{m2.current_hp}")

    # 第一杯（2026-09-23 用户令重做）：你受到的[回复]与失去的生命翻倍；
    # 旧「免疫癌变」（原钱袋效果）条款已废止 → 持有者照样癌变。
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "罪孽都市")
    engine.state.relics = [Relic(name="第一杯", effect="")]
    player = engine.state.player
    player.current_hp = 10
    heal = engine.state.apply_heal(player, 5)
    assert heal["heal_amount"] == 10 and player.current_hp == 20, "持有第一杯应使受到的回复翻倍"
    player.total_healed = engine.combat.cancer_threshold_of(player)
    hit = engine.combat.check_cancer(player)
    assert hit is not None and not player.is_alive, "第一杯不再免疫癌变"
    print("  ✓ 第一杯：回复翻倍，且不再免疫癌变")
    print("  ✓ 遗物效果测试通过")


def test_monster_phase_engine():
    """测试引擎自主运行怪物回合（道纹激活+攻击出手）"""
    print("\n=== 测试：怪物回合引擎化 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    st = GameState(); st.current_region = "罪孽都市"
    st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=4, attack_power=6)
    # 2026-10-25：【疯狂】遗物化，第二条改写仍在册的【必中】。
    for n,x in [("全力",3),("必中",3)]:
        m.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="",cost_formula="",effect_formula=""), x_value=x)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()

    # 第1回合：2026-09-15 用户令删除白板限制，怪物首回合即可发动道纹
    combat.round_start()  # current_round→1
    r1 = resolve_monster_phase(combat, {"打手": "全力"}, target_refs={"打手": "enemy:0"})
    # 【全力】2026-09-17 用户令重做：攻击力锁定为其[法限]，持续X（旧版"攻击力+X，持续∞"已废止）
    assert m.has_status("全力"), "强化应作为状态生效"
    assert m.effective_attack_power() == m.mana_limit, \
        f"强化后攻击力应锁定为法限{m.mana_limit}，实{m.effective_attack_power()}"
    assert len(r1) > 0, "怪物应有出手"
    print(f"  ✓ 第1回合：激活【强化3】，攻击力锁定为法限{m.mana_limit}，怪物出手{len(r1)}次，贾凡HP{st.player.current_hp} 速{st.player.current_speed}")

    # 第2回合：发动【必中3】（准则9：不同道纹同回合各至多一次）
    combat.round_start()  # current_round→2
    r2 = resolve_monster_phase(combat, {"打手": "必中"}, target_refs={"打手": "enemy:0"})
    assert r2, "怪物自主攻击必须有结算"
    print(f"  ✓ 第2回合：发动【必中3】，怪物自主攻击")
    print("  ✓ 怪物回合引擎化测试通过")


def test_spells_trigger():
    """测试反应型法术自动触发（后发制人/生生不息）"""
    print("\n=== 测试：法术触发 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance, Spell
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    def mkplayer(spells=[], dw=["庇护","再生","杀伐"]):
        p = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                   mana_limit=14, current_mana=14, speed_limit=8, current_speed=8)
        for n in dw:
            p.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
        for sn, req in spells:
            p.spells.append(Spell(name=sn, required_daowen=req, trigger_condition="", effect_flow=""))
        return p

    # 后发制人：受伤害前→庇护，应挡掉伤害
    st = GameState(); st.player = mkplayer([("后发制人",["庇护"])])
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=20)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine())
    r = combat.resolve_attack(m, st.player, spell_choices={
        "before": {"后发制人": {"use": True, "steps": [
            {"x": 10, "target_ref": "player:0", "dodge": False},
        ]}},
        "after": {},
    })
    assert st.player.current_hp == 60, f"后发制人应挡掉20伤，实HP{st.player.current_hp}"
    assert "spell_logs" in r and r["spell_logs"], "应触发后发制人"
    print(f"  ✓ 后发制人：受伤害前发动庇护，挡掉20伤(HP仍{st.player.current_hp})")

    # 生生不息：失血后→再生
    st2 = GameState(); st2.player = mkplayer([("生生不息",["再生"])])
    m2 = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=10)
    st2.enemies.append(m2)
    combat2 = CombatEngine(st2, DiceEngine())
    # 玩家速度设0避免闪避，确保实损触发失血后
    st2.player.current_speed = 0
    r2 = combat2.resolve_attack(m2, st2.player, spell_choices={
        "before": {},
        "after": {"生生不息": {"use": True, "steps": [
            {"x": 4, "target_ref": "player:0", "dodge": False},
        ]}},
    })
    assert st2.player.current_hp == 60, f"生生不息应奶回满，实HP{st2.player.current_hp}"
    print(f"  ✓ 生生不息：失血后发动再生，奶回满(HP{st2.player.current_hp})")
    print("  ✓ 法术触发测试通过")


def test_flying():
    """测试飞行免选中；旧裂变分次承伤机制已删除，不在此复活。"""
    print("\n=== 测试：飞行 ===")
    from engine.models import GameState, StatusEffect
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    # 飞行：玩家飞行，地面怪无法选中
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    st.player.is_flying = True
    m = Entity(name="地面怪", entity_type="怪物", blood_limit=50, current_hp=50, attack_count=1, attack_power=10)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine())
    r = combat.resolve_attack(m, st.player)
    assert r.get("cant_target") is True, "地面怪应无法选中飞行玩家"
    assert st.player.current_hp == 60, "飞行玩家不应受伤"
    print("  ✓ 飞行：地面怪无法选中飞行玩家(HP不变)")

    print("  ✓ 飞行免选中测试通过")


def test_huoxueyi_relic():
    """测试【活血衣】（2026-10-03 承接已删【活血】道纹）：受到攻击伤害后回复其一半"""
    print("\n=== 测试：活血衣（遗物） ===")
    from engine.models import GameState, Relic
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    st.relics = [Relic(name="活血衣", effect="")]
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=5)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()
    combat.round_start()
    # 玩家挨5点(不闪避)：立刻回复 ceil(5/2)=3 → HP60→52
    st.player.current_speed = 0
    combat.resolve_attack(m, st.player)
    assert st.player.current_hp == 58, f"活血衣：受击5点→55，即时回复ceil(5/2)=3→58，实{st.player.current_hp}"
    assert st.player.hp_lost_this_round == 5
    combat.round_end()  # 活血衣是即时回复，回终不再有追加回复
    assert st.player.current_hp == 58, f"回终不得再有活血回复，实{st.player.current_hp}"
    print(f"  ✓ 活血衣：受击5点→55，即时回复3→58；回终无追加回复")
    print("  ✓ 活血衣测试通过")


def test_events_system():
    """测试事件系统：解析/触发/结算"""
    print("\n=== 测试：事件系统 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    # 解析数量
    assert len(engine.event_pool.events) >= 25, f"应解析>=25事件，实{len(engine.event_pool.events)}"
    print(f"  ✓ 解析到{len(engine.event_pool.events)}个事件")

    # 探索触发（补精力）
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action":"探索"})
    assert r["success"], f"探索失败: {r}"
    ev_name = r["result"]["event"]
    assert r["result"]["options"], "事件应有选项"
    print(f"  ✓ 探索触发【{ev_name}】，{len(r['result']['options'])}个选项")

    # R20：不能越过当前事件直接结算祭坛；随后显式构造祭坛为当前事件再测效果。
    wrong_name = "祭坛" if ev_name != "祭坛" else "无名冢"
    wrong = engine.execute_action("resolve_event", {"event": wrong_name, "option_id": 1})
    assert not wrong["success"] and engine.event_pool.current == ev_name
    engine.event_pool.current = "祭坛"
    engine.state.shards = 100
    balance_before = engine.state.shards
    r2 = engine.execute_action("resolve_event", {
        "event": "祭坛", "option_id": 1, "x": 10, "multiplier": 1,
    })
    assert r2["success"], f"结算失败: {r2}"
    net_delta = r2["result"]["net_delta"]
    assert net_delta in (-10, 0, 10), f"倍率1的净变化必须在-10/0/+10中，实{net_delta}"
    assert engine.state.shards == balance_before + net_delta
    assert "祭坛" not in engine.event_pool.triggered, "祭坛可重复抵达，不永久标记已触发"
    assert "祭坛" in engine.event_pool.build_pool("扭曲都市"), "结算后祭坛应可再次探索"
    print(f"  ✓ 祭坛自选倍率1：净变化{net_delta:+d}，余额{engine.state.shards}")
    print("  ✓ 事件系统测试通过")


def test_death_page_legacy():
    """测试死者之页（2026-10-25：夺书不再是独立行动，按「视野内目标」通用机制处理）"""
    print("\n=== 测试：死者之页 ===")
    from engine.models import GameState
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=10)
    emp = Entity(name="追求者", entity_type="员工", blood_limit=96, current_hp=96, attack_count=8, attack_power=2)
    fri = Entity(name="阿岚", entity_type="朋友", blood_limit=40, current_hp=40, attack_count=2, attack_power=3)
    st.employees.append(emp); st.friends.append(fri)
    combat = CombatEngine(st, DiceEngine())

    # 没有专门的夺书/背叛判定入口：想夺书的人直接开战
    assert not hasattr(combat, "betrayal_candidates"), "夺书不应再有独立的引擎入口"

    st.player.is_alive = False; st.player.current_hp = 0
    legacy = {
        "text": "速度归零后受到致死攻击",
    }
    r3 = combat.trigger_death_legacy(legacy)
    assert r3["triggered"] and st.death_book_legacies == [legacy]
    print(f"  ✓ 死者之页：命零留单句遗言'{r3['legacy']['text'][:12]}...'")


def test_relics_five_more():
    """测试无所求/同魂笔（2026-10-13：【三相残韵盘】删除、【买路财】改制为逃跑行动，原两段已删）"""
    print("\n=== 测试：剩余遗物 ===")
    from engine.models import GameState, Relic
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    # 无所求：resolve_event拒绝+1属性点（2026-09-16 裁定：按《物品索引》
    # 原文入属性点池，不再由引擎折算成速限/法限）
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"t","blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    engine.state.relics = [Relic(name="无所求", effect="")]
    engine.event_pool.current = "祭坛"
    pool = engine.state.attribute_points
    sp = engine.state.player.speed_limit
    engine.execute_action("resolve_event", {"event":"祭坛","option_id":2})  # 当前祭坛只有投资/拒绝两项
    assert engine.state.attribute_points == pool + 1, "无所求拒绝应+1属性点"
    assert engine.state.player.speed_limit == sp, "属性点入池，不应直接加速限"
    print(f"  ✓ 无所求：选拒绝类选项+1属性点(池{pool}→{engine.state.attribute_points})")
    print("  ✓ 剩余遗物测试通过")


def test_evolution_yuanchu():
    """
    测试【进化】（原初X）与怪物的迷失豁免（异变仍累计，效果不中断）
    规则：原初X：代价：异变5X。选择一种自身未持有的原始怪物道纹，[战终]前视为持有
    （数值固定为本次X），借用的道纹发动时照常支付其自身代价。须处于困境，每场限一次。
    """
    print("\n=== 测试：进化（原初X）与迷失 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance
    from engine.combat import CombatEngine

    def mk_engine():
        engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
        })
        finish_initial_daowen(engine)
        # 裁定：原初X 借用池 = 轮回者当前持有的道纹，故须先给轮回者道纹
        for _n in ("必中", "全力", "杀伐"):
            engine.state.player.dao_wen[_n] = DaoWenInstance(
                dao_wen=DaoWen(name=_n, formula="", cost_type="消耗",
                               cost_formula="X", effect_formula=""), x_value=1)
        engine.combat.reset_monster_activation()
        engine.state.phase = "in_combat"
        return engine

    def mk_plight_monster(name="困境怪", hp=120, cur=30, atk=1, dw=None):
        """困境怪：生命≤30% + 攻击力极低（2个困境信号）"""
        m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=cur,
                   attack_count=2, attack_power=atk)
        for n, x in (dw or []):
            m.dao_wen[n] = DaoWenInstance(
                dao_wen=DaoWen(name=n, formula="", cost_type="代价", cost_formula="异变5X",
                               effect_formula="", is_monster_original=True), x_value=x)
        return m

    # ---- 1. 正常路径：困境怪物发动原初2借用【必中2】 ----
    engine = mk_engine()
    m = mk_plight_monster(dw=[("全力", 2)])   # 2026-10-03：【狂暴】删除，改用同为原始怪物道纹的【全力】
    engine.state.enemies.append(m)
    r = engine.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "必中", "x": 2})
    assert r["success"], f"正常进化应成功: {r}"
    assert m.mutation_count == 10, f"门票异变应为5×2=10，实{m.mutation_count}"
    assert "必中" in m.dao_wen and m.dao_wen["必中"].x_value == 2, "应借用【必中2】"
    assert "原初借用" in m.dao_wen["必中"].dao_wen.tags, "借用道纹应有原初借用标记"
    assert r["collapsed"] is False, "10层不应迷失"
    print(f"  ✓ 困境怪发动【原初2】：异变+10（当前{m.mutation_count}层），借用【必中2】至战终")

    # ---- 2. 边界：同场第二次进化 → 拒绝 ----
    r2 = engine.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "变形", "x": 1})
    assert not r2["success"] and "限一次" in r2["error"], f"同场二次进化应被拒绝: {r2}"
    assert m.mutation_count == 10, "被拒绝的进化不得扣异变"
    print(f"  ✓ 同场第二次进化被拒绝（每场限一次），异变仍为{m.mutation_count}层")

    # ---- 3. 边界：异变恰好到（阈值-1）层 → 存活且借用生效 ----
    T = Entity.MUTATION_COLLAPSE_THRESHOLD
    m29 = mk_plight_monster(name="临界怪")
    m29.mutation_count = T - 11
    engine.state.enemies.append(m29)
    r3 = engine.execute_action("declare_evolution", {"monster": "临界怪", "daowen": "必中", "x": 2})
    assert r3["success"] and r3["collapsed"] is False, f"{T-1}层应存活: {r3}"
    assert m29.mutation_count == T - 1 and m29.is_alive, f"{T-1}层应存活"
    print(f"  ✓ 异变{T-11}+10={T-1}层：存活，借用生效（迷失阈值{T}未达）")

    # ---- 4. 边界：异变恰好到旧阈值 → 怪物存活，借用照常生效 ----
    m30 = mk_plight_monster(name="迷失怪")
    m30.mutation_count = T - 10
    engine.state.enemies.append(m30)
    r4 = engine.execute_action("declare_evolution", {"monster": "迷失怪", "daowen": "必中", "x": 2})
    assert r4["success"] and r4["collapsed"] is False, f"怪物{T}层异变不触发迷失: {r4}"
    assert m30.mutation_count == T and m30.is_alive and m30.current_hp == 30
    assert "必中" in m30.dao_wen and m30.dao_wen["必中"].x_value == 2
    print(f"  ✓ 怪物异变{T-10}+10={T}层：不触发迷失，借用【必中】正常生效")

    # ---- 5. 非法输入：借用轮回者未持有的道纹 → 拒绝 ----
    engine2 = mk_engine()
    m_bad = mk_plight_monster(name="非法怪", dw=[("狂暴", 2)])
    engine2.state.enemies.append(m_bad)
    r5 = engine2.execute_action("declare_evolution", {"monster": "非法怪", "daowen": "愤怒", "x": 1})
    assert not r5["success"] and "不在轮回者当前持有的道纹中" in r5["error"], \
        f"借用轮回者未持有的道纹应被拒绝: {r5}"
    # ---- 非法输入：借用怪物自身已持有的道纹 → 拒绝 ----
    # 用轮回者也持有的"全力"，确保先通过"必须在轮回者道纹池内"这一关，
    # 从而真正命中"怪物已持有"的拒绝分支。
    from engine.models import DaoWen as _DW, DaoWenInstance as _DWI
    m_bad.dao_wen["全力"] = _DWI(dao_wen=_DW(name="全力", formula="", cost_type="代价",
                                             cost_formula="异变5X", effect_formula="",
                                             is_monster_original=True), x_value=1)
    r6 = engine2.execute_action("declare_evolution", {"monster": "非法怪", "daowen": "全力", "x": 1})
    assert not r6["success"] and "已持有" in r6["error"], f"借用已持有道纹应被拒绝: {r6}"
    # ---- 非法输入：X=0 → 拒绝 ----
    r7 = engine2.execute_action("declare_evolution", {"monster": "非法怪", "daowen": "必中", "x": 0})
    assert not r7["success"], f"X=0应被拒绝: {r7}"
    # ---- 非法输入：非困境 → 拒绝 ----
    m_fine = Entity(name="满状态怪", entity_type="怪物", blood_limit=120, current_hp=120,
                    attack_count=2, attack_power=10)
    engine2.state.enemies.append(m_fine)
    r8 = engine2.execute_action("declare_evolution", {"monster": "满状态怪", "daowen": "必中", "x": 1})
    assert not r8["success"] and "未陷入困境" in r8["error"], f"非困境进化应被拒绝: {r8}"
    assert m_bad.mutation_count == 0 and m_fine.mutation_count == 0, "被拒绝的进化均不扣异变"
    print(f"  ✓ 非法输入全部拒绝：转化道纹/已持有/X=0/非困境，且均不扣异变")

    # ---- 6. 原始道纹真实计费，偏爱每回始另加法限等量异变；怪物不迷失 ----
    st = GameState()
    st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                       speed_limit=8, current_speed=8)
    m_act = mk_plight_monster(name="计费怪", hp=120, cur=120, atk=6, dw=[("全力", 3)])   # 2026-10-03：狂暴删除 → 全力
    st.enemies.append(m_act)
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()
    combat.round_start()  # 第1回合（白板）
    combat.round_start()  # 第2回合：发动全力3
    r9 = resolve_monster_phase(combat, {"计费怪": "全力"})
    assert m_act.mutation_count == 15 + 2 * m_act.mana_limit, "两次偏爱回始 + 发动全力3的异变15"
    print(f"  ✓ 怪物发动【全力3】真实支付异变15层（当前{m_act.mutation_count}层）")

    # 越过旧迷失阈值仍可发动全力并攻击；回始遗物异变与道纹代价独立累计
    st2 = GameState()
    st2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                        speed_limit=8, current_speed=8)
    m_col = mk_plight_monster(name="自毁怪", hp=120, cur=120, atk=6, dw=[("全力", 3)])
    m_col.mutation_count = Entity.MUTATION_COLLAPSE_THRESHOLD - 15
    st2.enemies.append(m_col)
    combat2 = CombatEngine(st2, DiceEngine()); combat2.reset_monster_activation()
    combat2.round_start(); combat2.round_start()
    r10 = resolve_monster_phase(combat2, {"自毁怪": "全力"})
    assert m_col.mutation_count == Entity.MUTATION_COLLAPSE_THRESHOLD + 2 * m_col.mana_limit
    assert m_col.is_alive and m_col.current_hp == 120
    # 单行动契约：怪物一回合只有一次出手，本回合花在道纹上；再下一回合才轮到它普攻。
    combat2.round_start()
    r10b = resolve_monster_phase(combat2, {"自毁怪": None})
    assert st2.player.current_hp < 60, "怪物越过50层后仍应发动道纹并正常攻击"
    assert not any(e.get("collapsed") or e.get("lost") for e in r10b)
    assert not any(e.get("collapsed") or e.get("lost") for e in r10)
    print(f"  ✓ 怪物异变{m_col.mutation_count}层仍存活，正常攻击，玩家HP={st2.player.current_hp}")

    # ---- 7. 借用道纹：原初门票与首次发动各付一次，持续期间不再计费 ----
    st3 = GameState()
    st3.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                        speed_limit=8, current_speed=8)
    # 原初X 借用池 = 轮回者持有的道纹
    st3.player.dao_wen["必中"] = DaoWenInstance(
        dao_wen=DaoWen(name="必中", formula="", cost_type="消耗",
                       cost_formula="X", effect_formula=""), x_value=1)
    m_b = mk_plight_monster(name="借用怪", hp=120, cur=30, atk=1)  # 无自有道纹，仅借用
    st3.enemies.append(m_b)
    combat3 = CombatEngine(st3, DiceEngine()); combat3.reset_monster_activation()
    ev = combat3.execute_evolution(m_b, "必中", 2)
    assert ev["success"] and m_b.mutation_count == 10, f"原初2门票应为10层: {ev}"
    combat3.round_start(); combat3.round_start()
    resolve_monster_phase(combat3, {"借用怪": "必中"})  # 第2回合显式选择借用的减速2
    total1 = m_b.mutation_count
    assert total1 == 20 + 2 * m_b.mana_limit, "进化10 + 发动10 + 两次偏爱回始异变"
    assert m_b.is_alive, "20层应存活"
    # 2026-09-16 用户令：道纹递增（每次发动 X+2×副本阶级）已废止，
    # X 保持借用时写定的数值不变，重复发动按同一个 X 计费。
    assert m_b.dao_wen["必中"].x_value == 2, "递增已废止，X应保持借用的2"
    combat3.round_start()
    resolve_monster_phase(combat3, {"借用怪": "必中"})
    total2 = m_b.mutation_count
    assert total2 == total1 + m_b.mana_limit + 10 and m_b.is_alive, "回始偏爱与再次发动各自计入"
    assert m_b.dao_wen["必中"].x_value == 2
    print(f"  ✓ 借用门票与发动按X=2分别付10异变，偏爱回始另累计，最终{total2}层")
    print("  ✓ 进化（原初X）与迷失测试通过")


def test_evolution_plight_listing():
    """测试困境标注暴露给AI（裁定①：AI玩家决策调用——引擎只标注，AI自选是否进化及参数）"""
    print("\n=== 测试：进化困境标注（available_actions暴露）===")
    from engine.models import DaoWen, DaoWenInstance

    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine)
    engine.combat.reset_monster_activation()
    engine.state.phase = "in_combat"

    # 困境怪：生命≤30%+攻击力极低（2信号），已持有狂暴2，异变10层
    m = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
               attack_count=2, attack_power=1)
    m.dao_wen["狂暴"] = DaoWenInstance(
        dao_wen=DaoWen(name="狂暴", formula="", cost_type="代价", cost_formula="异变5X",
                       effect_formula="", is_monster_original=True), x_value=2)
    m.mutation_count = 10
    engine.state.enemies.append(m)

    # ---- 1. 正常路径：行动列表出现evolution项，参数由引擎算好 ----
    actions = engine.get_available_actions()
    evo = next((a for a in actions["actions"] if a.get("action_type") == "declare_evolution"), None)
    assert evo is not None, "战斗行动列表应包含evolution项"
    assert evo["available"] is True, "存在困境怪物时evolution应可用"
    assert len(evo["plight_monsters"]) == 1, "应恰好1只困境怪"
    info = evo["plight_monsters"][0]
    assert info["monster"] == "困境怪"
    # 裁定：原初X 的借用池改为"轮回者当前持有的道纹"（不再是7种原始怪物道纹）
    assert set(info["borrowable_daowen"]) <= set(engine.state.player.dao_wen), \
        "借用池必须是轮回者持有的道纹的子集"
    assert "狂暴" not in info["borrowable_daowen"], "怪物已持有的道纹不可借用"
    assert info["max_x_by_mutation"] is None, "怪物进化X不受异变限制，用None而不是0表达"
    assert info["mutation_lost_exempt"] is True
    print(f"  ✓ evolution项已暴露：困境怪（信号{info['difficulty_signals']}），"
          f"借用池由玩家道纹决定，怪物不受迷失X限制={info['max_x_by_mutation']}")

    # ---- 2. 边界：仅1个劣势信号 → 判定困境（裁定⑦：探针≥1） ----
    m2 = Entity(name="半血怪", entity_type="怪物", blood_limit=120, current_hp=120,
                attack_count=2, attack_power=1)  # 只有攻击力极低1个信号
    engine2 = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine2.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine2)
    engine2.combat.reset_monster_activation()
    engine2.state.phase = "in_combat"
    engine2.state.enemies.append(m2)
    evo2 = next(a for a in engine2.get_available_actions()["actions"] if a.get("action_type") == "declare_evolution")
    assert evo2["available"] is True and len(evo2["plight_monsters"]) == 1, \
        "裁定⑦后仅1个劣势信号即判定困境，应列单"
    assert evo2["plight_monsters"][0]["monster"] == "半血怪"
    # 边界补充：0个信号（满状态）→ 不可用
    m2b = Entity(name="满状态怪", entity_type="怪物", blood_limit=120, current_hp=120,
                 attack_count=2, attack_power=10)
    engine2b = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine2b.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine2b)
    engine2b.combat.reset_monster_activation()
    engine2b.state.phase = "in_combat"
    engine2b.state.enemies.append(m2b)
    evo2b = next(a for a in engine2b.get_available_actions()["actions"] if a.get("action_type") == "declare_evolution")
    assert evo2b["available"] is False and evo2b["plight_monsters"] == [], \
        "0个劣势信号不应判定困境"
    print("  ✓ 探针口径（裁定⑦）：1个劣势信号即列单（半血怪），0个信号不可用")

    # ---- 3. 边界：已进化过的怪物不再列出；死亡怪物不列出 ----
    engine3 = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine3.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    finish_initial_daowen(engine3)
    engine3.combat.reset_monster_activation()
    engine3.state.phase = "in_combat"
    m3 = Entity(name="已进化怪", entity_type="怪物", blood_limit=120, current_hp=30,
                attack_count=2, attack_power=1)
    engine3.combat._monster_evolved.add(id(m3))
    m4 = Entity(name="死亡怪", entity_type="怪物", blood_limit=120, current_hp=0,
                attack_count=2, attack_power=1)
    m4.is_alive = False
    engine3.state.enemies.extend([m3, m4])
    evo3 = next(a for a in engine3.get_available_actions()["actions"] if a.get("action_type") == "declare_evolution")
    assert evo3["available"] is False and evo3["plight_monsters"] == [], \
        "已进化/已死亡怪物均不应列为困境可进化"
    print("  ✓ 已进化（二选一已用）与已死亡怪物均不出现在标注中")
    print("  ✓ 进化困境标注测试通过")


def test_original_daowen_only_charges_mutation_on_activation():
    """原始怪物道纹支付异变5X的时机口径（README 怪物准则9，2026-08-21 统一）：

    - 每次【实际发动】该道纹时支付一次异变5X（X=该次发动时递增后的数值）；
    - 效果持续期间（该回合及后续回合未再次发动该道纹）不再重复计费；
    - 若再次发动（重施），按递增后的新X再付一次（见
      test_monster_daowen_escalation.py::test_original_daowen_escalates_and_pays_scaled_mutation）。
    """
    print("\n=== 测试：原始怪物道纹仅首次发动计费 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    def mk(name, dw):
        # 2026-09-16：怪物[法限]即法力池，法力同时就是[攻击力]。
        m = Entity(name=name, entity_type="怪物", blood_limit=200, current_hp=200,
                   attack_count=1, attack_power=5)
        for n, x in dw:
            m.dao_wen[n] = DaoWenInstance(
                dao_wen=DaoWen(name=n, formula="", cost_type="代价", cost_formula="异变5X",
                               effect_formula="", is_monster_original=True), x_value=x)
        return m

    def mkbed(m):
        st = GameState()
        st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                           speed_limit=8, current_speed=8)
        st.enemies.append(m)
        c = CombatEngine(st, DiceEngine()); c.reset_monster_activation()
        return st, c

    # 正常：必中2激活只付X；持续期间不重复计费（后续回合改发免费的庇护，
    # 准则9下怪物有合法道纹选项时必须出招，故给填充道纹而非空过）。
    m1 = mk("持续怪", [("必中", 2)])
    m1.dao_wen["庇护"] = DaoWenInstance(
        dao_wen=DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
                       effect_formula=""), x_value=1)
    _, c1 = mkbed(m1)
    # 删除白板后怪物每回合都有合法选项就必须出招，首回合先发填充道纹【庇护】
    c1.round_start(); resolve_monster_phase(c1, {"持续怪": "庇护"})
    c1.round_start(); resolve_monster_phase(c1, {"持续怪": "必中"})
    assert m1.mutation_count == 10 + 2 * m1.mana_limit and m1.is_alive
    for _ in range(3):
        before = m1.mutation_count
        c1.round_start(); resolve_monster_phase(c1, {"持续怪": "庇护"})
        assert m1.mutation_count == before + m1.mana_limit and m1.is_alive
    print("  ✓ 减速2发动支付异变10，持续期间不再支付代价，偏爱回始仍累计异变")

    # 边界：次数型必中同样只在激活时付一次，不存在额外豁免分支。
    m2 = mk("次数怪", [("必中", 3)])
    m2.dao_wen["庇护"] = DaoWenInstance(
        dao_wen=DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
                       effect_formula=""), x_value=1)
    _, c2 = mkbed(m2)
    c2.round_start(); resolve_monster_phase(c2, {"次数怪": "庇护"})
    c2.round_start(); resolve_monster_phase(c2, {"次数怪": "必中"})
    c2.round_start(); resolve_monster_phase(c2, {"次数怪": "庇护"})
    assert m2.mutation_count == 15 + 3 * m2.mana_limit and m2.is_alive
    print("  ✓ 必中3首次支付异变15，未再发动则不再计费")

    # 怪物免于迷失：首次发动越过50层，效果不中断且角色存活。
    m3 = mk("临界怪", [("必中", 2)])
    m3.mutation_count = Entity.MUTATION_COLLAPSE_THRESHOLD - 10
    m3.dao_wen["庇护"] = DaoWenInstance(
        dao_wen=DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
                       effect_formula=""), x_value=1)
    _, c3 = mkbed(m3)
    c3.round_start(); resolve_monster_phase(c3, {"临界怪": "庇护"})
    c3.round_start(); result = resolve_monster_phase(c3, {"临界怪": "必中"})
    assert m3.is_alive and m3.mutation_count == Entity.MUTATION_COLLAPSE_THRESHOLD + 2 * m3.mana_limit
    assert not any(entry.get("collapsed") or entry.get("lost") for entry in result)
    print("  ✓ 怪物首次发动越过50层异变仍存活，效果不中断")


def test_consumable_mutation_wiring():
    """
    测试裁定⑧（A4全量）：普通消耗品中"获得异变N"统一走 Entity.add_mutation；
    非怪物角色达50层仍触发【迷失】，怪物免于该事件。
    """
    print("\n=== 测试：消耗品异变统一入口（裁定⑧） ===")
    from engine.models import Consumable

    T = Entity.MUTATION_COLLAPSE_THRESHOLD

    def mk_engine(mut=0):
        engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
        })
        finish_initial_daowen(engine)
        engine.state.player.mutation_count = mut
        return engine

    # ---- 1. 正常路径：残骸（1/1）"恢复20生命并获得异变10" → 异变+10，耐久归零 ----
    engine = mk_engine()
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"], f"使用残骸应成功: {r}"
    p = engine.state.player
    assert p.mutation_count == 10, f"残骸应+10异变，实{p.mutation_count}"
    assert r["result"]["mutation"]["mutation_total"] == 10 and not r["result"]["mutation"]["collapsed"]
    assert engine.state.consumables[0].is_depleted, "残骸（1/1）应用尽"
    print(f"  ✓ 残骸使用：异变+10（当前{p.mutation_count}层，阈值{T}），耐久1→0")

    # ---- 2. 边界：T-10层使用残骸 → 恰好T层触发迷失，命零 ----
    engine2 = mk_engine(mut=T - 10)
    engine2.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r2 = engine2.execute_action("consume_item", {"name": "残骸"})
    assert r2["success"] and r2["result"]["mutation"]["collapsed"], f"{T-10}+10应迷失: {r2}"
    p2 = engine2.state.player
    assert p2.mutation_count == T and not p2.is_alive and p2.current_hp == 0, \
        "轮回者迷失应直接命零"
    print(f"  ✓ 边界：{T-10}层+异变10={T}层触发迷失，轮回者命零（{r2['result']['mutation'].get('note','')}）")

    # ---- 3. 非法/对照：不含异变的普通消耗品不触碰异变；找不到的消耗品拒绝 ----
    engine3 = mk_engine()
    engine3.state.consumables.append(Consumable(
        name="无异变测试品", effect="回复0", current_uses=1, max_uses=1))
    r3 = engine3.execute_action("consume_item", {"name": "无异变测试品"})
    assert r3["success"] and r3["result"]["mutation"] is None, "无异变文本应不触碰异变"
    assert engine3.state.player.mutation_count == 0
    r4 = engine3.execute_action("consume_item", {"name": "不存在的道具"})
    assert not r4["success"], "找不到的消耗品应拒绝"
    assert engine3.state.player.mutation_count == 0, "被拒绝的使用不得加异变"
    print("  ✓ 对照：普通消耗品mutation=None且不触及异变；不存在道具拒绝且不扣层")
    print("  ✓ 消耗品异变统一入口测试通过")


def test_twisted_tool_library():
    """
    测试裁定⑬：扭曲都市完成事件附赠【发现】工具库消耗品。
    """
    print("\n=== 测试：扭曲工具库附赠发现（裁定⑬） ===")
    from engine.models import Consumable
    from engine.api import TWISTED_TOOL_LIBRARY

    def mk(region="扭曲都市"):
        engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6
        })
        finish_initial_daowen(engine)
        _choose_region(engine, region)
        return engine

    ev_name = None
    engine = mk()
    ev_name = next(n for n, ev in engine.event_pool.events.items()
                   if ev["region"] == "扭曲都市")

    # ---- 1. 正常路径：扭曲都市结算事件 → 随机列3件，再显式选1件 ----
    engine.event_pool.current = ev_name
    r = engine.execute_action("resolve_event", {"event": ev_name, "option_id": 1})
    assert r["success"], f"事件结算失败: {r}"
    bonus = r["result"].get("附赠发现")
    assert bonus is not None and bonus["等待选择"] is True
    assert len(bonus["候选"]) <= 3
    chosen = bonus["候选"][-1]
    picked = engine.execute_action("choose_discovered_item", {"item_name": chosen})
    assert picked["success"]
    dur_expected = TWISTED_TOOL_LIBRARY[chosen][0]
    got_item = next(c for c in engine.state.consumables if c.name == chosen)
    assert got_item.current_uses == dur_expected
    print(f"  ✓ 事件【{ev_name}】附赠发现：候选{bonus['候选']}→显式选择【{chosen}】")

    # ---- 2. 非法输入：不在候选的显式选择必须拒绝，不得回退第一件 ----
    engine2 = mk()
    ev2 = next(n for n, ev in engine2.event_pool.events.items()
               if ev["region"] == "扭曲都市" and n != ev_name)
    engine2.event_pool.current = ev2
    reject_option = next(o for o in engine2.event_pool.events[ev2]["options"]
                         if engine2._is_reject_option_text(o["text"]))
    r2 = engine2.execute_action("resolve_event", {"event": ev2, "option_id": reject_option["id"]})
    assert r2["success"]
    bad = engine2.execute_action("choose_discovered_item", {"item_name": "不存在的道具"})
    assert not bad["success"] and engine2.state.pending_item_choices
    print("  ✓ 非候选选择被拒绝，候选保持待选")

    # ---- 3. 边界：8件全持有 → 再无附赠；非扭曲副本也不附赠 ----
    engine3 = mk()
    for n, (dur, txt) in TWISTED_TOOL_LIBRARY.items():
        engine3.state.consumables.append(Consumable(name=n, effect=txt,
                                                    current_uses=dur, max_uses=dur))
    engine3.event_pool.current = ev_name
    r3 = engine3.execute_action("resolve_event", {"event": ev_name, "option_id": 1})
    assert r3["success"] and "附赠发现" not in r3["result"], "全持有后不应再附赠"
    engine4 = mk(region="龙心谷")
    ev4 = next(n for n, ev in engine4.event_pool.events.items() if ev["region"] == "龙心谷")
    engine4.event_pool.current = ev4
    r4 = engine4.execute_action("resolve_event", {"event": ev4, "option_id": 1})
    assert r4["success"] and "附赠发现" not in r4["result"], "非扭曲副本不应附赠工具库"
    print("  ✓ 边界：8件全持有不再附赠；龙心谷事件不附赠")
    print("  ✓ 扭曲工具库附赠发现测试通过")


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("第四宇宙游戏引擎 - 测试套件")
    print("=" * 60)
    
    os.makedirs("data", exist_ok=True)
    
    tests = [
        test_setup,
        test_daowen_calculations,
        test_resonance,
        test_combat,
        test_dice,
        test_dm_rulings,
        test_full_flow,
        test_sculpture_and_proliferation,
        test_daowen_effects_wired,
        test_out_of_combat_actions,
        test_relic_effects,
        test_monster_phase_engine,
        test_spells_trigger,
        test_flying_and_split,
        test_huoxue,
        test_events_system,
        test_death_page_legacy,
        test_relics_five_more,
        test_evolution_yuanchu,
        test_evolution_plight_listing,
        test_original_daowen_only_charges_mutation_on_activation,
        test_consumable_mutation_wiring,
        test_twisted_tool_library,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"\n  ✗ 失败: {test.__name__}")
            print(f"    错误: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)



def test_shuwu_codex_grants_20_shards():
    """回归：遗忘书屋禁忌法典opt2（失忆1+自选遗物与20碎片）必须到账20碎片（r7实测漏发bug）。"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_shuwu.db", rng_seed=7)
    engine.execute_action("setup_attributes", {"name": "t", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    held = list(engine.state.player.dao_wen.keys())
    assert held, "玩家应持有初始道纹供失忆"
    pool_name = engine.state.relics_pool[0].name
    engine.state.energy = 3
    engine.event_pool.current = "遗忘书屋"
    before = engine.state.shards
    r = engine.execute_action("resolve_event", {"event": "遗忘书屋", "option_id": 2,
                                                "daowen_names": [held[0]], "relic_name": pool_name})
    assert r["success"], r
    assert engine.state.shards == before + 20, f"应+20碎片，实{engine.state.shards - before}"
    assert held[0] not in engine.state.player.dao_wen, "失忆道纹应被移除"
    assert any(x.name == pool_name for x in engine.state.relics), "自选遗物应到账"
