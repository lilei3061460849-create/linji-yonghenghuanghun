"""
怪物代价类道纹支付与防复读规则测试。
规则：怪物与轮回者、微光者同口径持有[法限]（2026-09-16 用户令废止了旧条文
「怪物不持有法力、发动道纹不支付法力」）；本文件聚焦**代价类**道纹（代价类型
非【消耗】，不走法力制），发动时必须照常支付对应代价：
- 发动【冷却X】道纹（如束缚/全速）后立即进入冷却，同场内禁止复读；
  怪物使用冷却代价类的道纹 X 值只能≤1（2026-10-05 用户令，面板更大也按X=1结算）；
- 发动【流血X】（如连击）扣除自身生命，生命不足无法发动；
- 发动【衰老X】扣除血限，发动【疲惫X】扣除速度，发动【异变5X】累加异变。
"""
import pytest
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


from tests.setup_support import begin_battle, finish_initial_daowen
def _setup_engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "test.db"), rng_seed=4)
    e.execute_action("setup_attributes", {
        "name": "贾希希", "blood_points": 5, "speed_points": 8, "mana_points": 12
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": s["result"]["relic_choices"][0]})
    # 开局随机遗物与支付断言无关；setup 完成后清空，避免千荆甲反噬混入流血成本。
    e.state.relics.clear()
    return e


def test_monster_cooldown_daowen_sets_cooldown_and_cannot_repeat(tmp_path):
    """正常路径：怪物发动全速后，道纹进入冷却，后续回合不再列出，禁止复读。"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1
        })
    started = begin_battle(e)
    assert started["success"], started
    m = Entity("靶怪", "怪物", blood_limit=80, current_hp=80, attack_count=1, attack_power=1)
    e.state.enemies[:] = [m]

    # 第1回合（空手：怪物此时没有道纹）
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp1 = e.execute_action("prepare_monster_phase", {})
    actor1 = pmp1["result"]["actors"][0]
    assert actor1["daowen_required"] is False, "怪物无道纹时不得要求提交道纹"
    e.execute_action("resolve_monster_phase", {
        "token": pmp1["result"]["token"],
        "choices": [{
            "actor_ref": actor1["actor_ref"],
            "daowen": None,
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor1["base_hits_per_attack"]}] * actor1["base_attack_actions"]
        }]
    })
    e.execute_action("round_end", {})

    # 第2回合：怪物获得全速（2026-09-15 删除白板后，怪物持有道纹即当回合可发动）
    m.dao_wen["全速"] = DaoWenInstance(
        DaoWen(name="全速", formula="", cost_type="冷却", cost_formula="X", effect_formula=""),
        x_value=3
    )
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp2 = e.execute_action("prepare_monster_phase", {})
    actor2 = pmp2["result"]["actors"][0]
    daowen_names = [opt["name"] for opt in actor2["daowen_options"]]
    assert "全速" in daowen_names

    # 提交发动全速
    e.execute_action("resolve_monster_phase", {
        "token": pmp2["result"]["token"],
        "choices": [{
            "actor_ref": actor2["actor_ref"],
            "daowen": {"name": "全速", "target_ref": "player:0", "dodge": False, "blood_shadow": False},
            "attack_actions": []
        }]
    })
    e.execute_action("round_end", {})

    # 验证（2026-10-05 用户令口径）：怪物使用冷却代价类道纹 X 值只能≤1——
    # 面板即便写 X=3，也一律按 X=1 结算，进入冷却1场；can_use 为 False
    assert m.dao_wen["全速"].cooldown_remaining == 1
    assert m.dao_wen["全速"].can_use() is False

    # 下一回合（第3回合）：prepare 不应再列出全速（防复读）
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp3 = e.execute_action("prepare_monster_phase", {})
    actor3 = pmp3["result"]["actors"][0]
    daowen_names3 = [opt["name"] for opt in actor3["daowen_options"]]
    assert "全速" not in daowen_names3, "冷却中的道纹不得再次列为可用选项，严禁复读"


def test_monster_bleed_cost_daowen_deducts_monster_hp(tmp_path):
    """正常路径：怪物发动连击必须扣除自身生命（支付流血代价）。"""
    e = _setup_engine(tmp_path)
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1
        })
    started = begin_battle(e)
    assert started["success"], started
    m = Entity("靶怪", "怪物", blood_limit=80, current_hp=80, attack_count=1, attack_power=1)
    e.state.enemies[:] = [m]

    # 第1回合（空手：怪物此时没有道纹）
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp1 = e.execute_action("prepare_monster_phase", {})
    actor1 = pmp1["result"]["actors"][0]
    assert actor1["daowen_required"] is False, "怪物无道纹时不得要求提交道纹"
    e.execute_action("resolve_monster_phase", {
        "token": pmp1["result"]["token"],
        "choices": [{
            "actor_ref": actor1["actor_ref"],
            "daowen": None,
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor1["base_hits_per_attack"]}] * actor1["base_attack_actions"]
        }]
    })
    e.execute_action("round_end", {})

    # 第2回合：怪物获得连击后发动（删除白板后，持有即可发动）
    m.dao_wen["连击"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="流血", cost_formula="X", effect_formula=""),
        x_value=5
    )
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    hp_after_player_hit = m.current_hp

    pmp2 = e.execute_action("prepare_monster_phase", {})
    actor2 = pmp2["result"]["actors"][0]
    assert "连击" in [opt["name"] for opt in actor2["daowen_options"]]

    e.execute_action("resolve_monster_phase", {
        "token": pmp2["result"]["token"],
        "choices": [{
            "actor_ref": actor2["actor_ref"],
            "daowen": {"name": "连击", "target_ref": "player:0", "dodge": False, "blood_shadow": False},
            "attack_actions": []
        }]
    })

    # 验证：怪物生命真实扣除了连击5的代价（2026-10-22 连击流血价5X：5击=25点）
    assert m.current_hp == hp_after_player_hit - 25, f"怪物发动连击5应扣25生命，实扣{hp_after_player_hit - m.current_hp}"


def test_monster_cannot_cast_when_hp_insufficient_for_cost(tmp_path):
    """边界条件/错误输入：怪物当前生命不足以支付代价时，无法发动代价道纹。"""
    e = _setup_engine(tmp_path)
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1
        })
    started = begin_battle(e)
    assert started["success"], started
    m = Entity("靶怪", "怪物", blood_limit=80, current_hp=10, attack_count=1, attack_power=1)
    m.dao_wen["连击"] = DaoWenInstance(
        DaoWen(name="连击", formula="", cost_type="流血", cost_formula="X", effect_formula=""),
        x_value=50
    )
    e.state.enemies[:] = [m]

    e.execute_action("round_start", {"relic_choices": {}})
    assert e.execute_action("skip", {"actor_ref": "player:0"})["success"]
    pmp = e.execute_action("prepare_monster_phase", {})
    assert pmp["success"], pmp
    actor = pmp["result"]["actors"][0]
    
    # 尝试非法强制提交付不起代价的道纹，必须被拒绝或跳过
    res = e.execute_action("resolve_monster_phase", {
        "token": pmp["result"]["token"],
        "choices": [{
            "actor_ref": actor["actor_ref"],
            "daowen": {"name": "连击", "target_ref": "player:0", "dodge": False, "blood_shadow": False},
            "attack_actions": []
        }]
    })
    # 应当被拒绝或结算为跳过，怪物不得自杀
    assert res["success"] is False or any(d.get("daowen_skipped") for d in res.get("result", {}).get("details", []))
    assert m.is_alive is True
