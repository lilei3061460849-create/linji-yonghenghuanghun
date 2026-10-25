"""
pytest 风格测试 - 里程碑4b：出手预算校验

DM裁定记录：
- 出手次数：所有角色都按`action_count`（基础2次 +【疯狂】/【无力】/【蓄锐·增】等修正）；
  怪物走独立的 run_monster_phase（基础 2 槽），不受此约束。
- 普攻本身不消耗速度；速度消耗只来自正文明确的闪避等机制。
- 消耗1出手的动作：attack、use_daowen(含指挥朋友/员工)、deploy_employee(消耗玩家的出手)、
  declare_wish；declare_escape（逃跑）一次付清5出手（2026-10-13 改制）。
- 不消耗出手：consume_item(原文明确)、use_resonance(可任意时刻插队)。
- 预算按回合重置(round_start时归零本回合已用出手数)。

运行方式：
    python -m pytest tests/test_action_budget.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.attack_support import resolve_attack as resolve_player_attack
from tests.monster_phase_support import resolve_monster_phase
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, Consumable


def _give_daowen(entity: Entity, name: str):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _new_engine(db_suffix: str, speed_points: int = 8) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_budget_{db_suffix}.db", rng_seed=1)
    # DM裁定 2026-09-10：2属性点=1速限=1法限 → 速/法点数须为偶数，奇数余点归血限
    mana_points = 6
    blood_points = 25 - speed_points - mana_points  # 属性点总和必须=25
    engine.execute_action("setup_attributes",
                           {"blood_points": blood_points, "speed_points": speed_points, "mana_points": mana_points})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})  # 雇佣为罪孽都市专属
    choices = setup["result"]["relic_choices"]
    engine.execute_action("choose_discovered_relic", {"relic_name": choices[0]})
    engine.state.energy = 0
    relic_choices = {}
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    engine.execute_action("battle_start", {"relic_choices": relic_choices})
    # battle_start会自动出怪，这里替换为受控的单一测试怪物，保证断言确定性
    engine.state.enemies.clear()
    # DM裁定 2026-09-10：攻力=当前法力，而下方会把 current_mana 设成 999，
    # 单次攻击即 攻次×999 伤害；靶怪血量必须远高于此，否则第一击就打死它，
    # 后续「没有合法攻击目标」会把出手预算的断言全部带偏。
    engine.state.enemies.append(Entity(name="靶怪", entity_type="怪物",
                                        blood_limit=10 ** 7, current_hp=10 ** 7,
                                        attack_count=1, attack_power=1))
    engine.execute_action("round_start", {})
    engine.state.player.current_mana = 999
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_player_action_count_formula_and_budget_enforced():
    """正常路径：轮回者出手预算固定2次（DM裁定 2026-09-10，不再由速限换算），用满后第3次被拒绝"""
    engine = _new_engine("player_budget", speed_points=8)
    player = engine.state.player
    assert player.action_count == 2
    for i in range(2):
        r = resolve_player_attack(engine, player.name, [])
        assert r["success"] is True, f"第{i+1}次攻击应成功: {r}"
    r4 = resolve_player_attack(engine, player.name, [])
    assert r4["success"] is False
    assert "出手已用完" in r4["error"]


def test_budget_resets_at_round_start():
    """正常路径：用完预算后，下一回合round_start必须重置，可以继续行动"""
    engine = _new_engine("reset_budget")   # 轮回者固定2次出手
    player = engine.state.player
    assert player.action_count == 2
    r1 = resolve_player_attack(engine, player.name, [])
    assert r1["success"] is True
    resolve_player_attack(engine, player.name, [])   # 第2次用满预算
    r2 = resolve_player_attack(engine, player.name, [])
    assert r2["success"] is False

    resolve_monster_phase(engine.combat, {"靶怪": None})
    engine.state.combat_subphase = "await_round_end"  # 单元测试直接调用CombatEngine，手动同步API子阶段
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    r3 = resolve_player_attack(engine, player.name, [])
    assert r3["success"] is True, "新回合应重置出手预算"


def test_ally_action_count_is_fixed_two_like_everyone():
    """2026-09-16 用户令：全体角色出手次数固定 2 次，与攻击次数/速限都无关。

    旧口径「微光者出手 = 攻击次数/3」已废止——该式会让高攻次微光者白拿第 3、4 次出手，
    违反"出手不再与攻击次数挂钩"的裁定。唯一额外来源是遗物（【疯狂】+X 照旧）。
    """
    engine = _new_engine("ally_formula")
    friend = Entity(name="力士", entity_type="朋友", blood_limit=40, current_hp=40,
                     attack_count=7, attack_power=3, speed_limit=0)
    engine.state.friends.append(friend)
    assert friend.action_count == 2, "出手固定2次，攻次7不再推导出3"


def test_deploy_employee_consumes_player_budget_not_employee():
    """正常路径：deploy_employee扣的是玩家的出手，不是被部署员工的出手"""
    engine = _new_engine("deploy_budget")   # 玩家固定2次出手
    engine.state.employees.append(Entity(
        name="小李", entity_type="员工", blood_limit=96, current_hp=96,
        attack_count=4, attack_power=8, is_deployed=False,
    ))
    player = engine.state.player
    r = engine.execute_action("deploy_employee", {"name": "小李"})
    assert r["success"] is True
    assert player.actions_used_this_round == 1
    resolve_player_attack(engine, player.name, [])   # 第2次用满
    r2 = resolve_player_attack(engine, player.name, [])
    assert r2["success"] is False, "玩家2次出手已被 deploy_employee + 攻击 用掉，不应再能攻击"


def test_consume_item_and_resonance_do_not_consume_budget():
    """正常路径：消耗品与残韵不占用出手预算，用完全部出手后仍可使用"""
    engine = _new_engine("no_budget_actions")   # 固定2次出手
    player = engine.state.player
    player.dao_wen["杀伐"] = DaoWenInstance(DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.consumables.append(Consumable(name="急救箱", effect="使自身获得[回复25]", current_uses=2, max_uses=2))
    for _ in range(player.action_count):           # 用光全部出手
        resolve_player_attack(engine, player.name, [])
    r_item = engine.execute_action("consume_item", {"name": "急救箱"})
    assert r_item["success"] is True, "消耗品不应受出手预算限制"

    engine.state.resonance["反转"] = 1
    r_res = engine.execute_action("use_resonance", {"source_daowen": "杀伐", "resonance_type": "反转",
                                                 "target_daowen": "再生"})
    assert r_res["success"] is True, f"残韵可任意时刻插队使用，不应受出手预算限制: {r_res}"


# ========================================================================
# 边界条件
# ========================================================================

def test_ally_command_daowen_shares_same_budget_pool_as_attack():
    """边界：同一实体的攻击与道纹共用同一份出手预算，不是分别独立计数"""
    engine = _new_engine("shared_pool")   # 固定2次出手
    player = engine.state.player
    player.dao_wen["杀伐"] = DaoWenInstance(DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    r1 = resolve_player_attack(engine, player.name, [])
    assert r1["success"] is True
    r2 = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": "靶怪"})
    assert r2["success"] is True, "第2次出手仍可发动道纹（与攻击共用同一份预算）"
    r3 = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": "靶怪"})
    assert r3["success"] is False, "2次出手用完后，再发动道纹应被拒绝"


def test_failed_target_lookup_does_not_consume_budget():
    """边界：因攻击者不存在而提前失败的行动，不应消耗任何人的出手（不能因为无效指令白白扣掉一次机会）"""
    engine = _new_engine("no_consume_on_fail")
    player = engine.state.player
    r_fail = resolve_player_attack(engine, "不存在的攻击者", [])
    assert r_fail["success"] is False
    assert player.actions_used_this_round == 0, "查找攻击者失败不应消耗任何人的出手"
    # 验证出手预算确实完好：随后一次正常攻击应能成功
    r_ok = resolve_player_attack(engine, player.name, [])
    assert r_ok["success"] is True


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_declare_wish_and_escape_consume_budget_and_reject_when_exhausted():
    """错误输入：出手耗尽后声明许愿/逃跑必须被拒绝"""
    engine = _new_engine("wit_escape_budget")   # 固定2次出手
    player = engine.state.player
    for _ in range(player.action_count):        # 用光全部出手
        resolve_player_attack(engine, player.name, [])
    r_wit = engine.execute_action("declare_wish", {"wish_text": "愿望测试", "target_ref": "enemy:0"})
    assert r_wit["success"] is False
    assert "出手已用完" in r_wit["error"]

    # 2026-10-13 逃跑改制：一次付清5次出手；剩0次时拒绝且一次不扣。
    used_before = player.actions_used_this_round
    r_escape = engine.execute_action("declare_escape", {})
    assert r_escape["success"] is False
    assert "一次付清5次出手" in r_escape["error"]
    assert player.actions_used_this_round == used_before


def test_escape_requires_five_remaining_and_consumes_atomically():
    """边界：剩2次出手时逃跑被拒（5次是硬门槛，不足一次不扣）；预算≥5时一次扣5。"""
    engine = _new_engine("escape_atomic")
    player = engine.state.player
    assert player.action_count < 5
    used_before = player.actions_used_this_round
    r = engine.execute_action("declare_escape", {})
    assert r["success"] is False
    assert "一次付清5次出手" in r["error"]
    assert player.actions_used_this_round == used_before, "原子消费：不足时一次不扣"

    from engine.models import Relic
    engine.state.relics.append(Relic(name="癫狂之脑", effect=""))   # 出手预算+1
    player.actions_used_this_round = 0
    # 预算仍不足5（2+1=3）：继续被拒
    r2 = engine.execute_action("declare_escape", {})
    assert r2["success"] is False
    assert player.actions_used_this_round == 0


def test_deploy_employee_rejected_when_player_budget_exhausted():
    """错误输入：玩家出手耗尽后不能再派遣员工，必须报错而不是免费执行"""
    engine = _new_engine("deploy_no_budget")   # 固定2次出手
    engine.state.employees.append(Entity(
        name="小周", entity_type="员工", blood_limit=96, current_hp=96,
        attack_count=4, attack_power=8, is_deployed=False,
    ))
    player = engine.state.player
    for _ in range(player.action_count):        # 用光全部出手
        resolve_player_attack(engine, player.name, [])
    r = engine.execute_action("deploy_employee", {"name": "小周"})
    assert r["success"] is False
    emp = next(e for e in engine.state.employees if e.name == "小周")
    assert emp.is_deployed is False, "预算不足时不应部署成功"


def test_rules_text_gives_every_role_the_same_base_two_slots():
    """规则正文的[速限]条目必须与引擎一致：全体基础 2 次，不再有攻击次数/3 的旧式。

    2026-10-25：正文残留「[朋友]/[员工] = [攻击次数] / 3」，而 Entity.action_count
    对所有角色都是 2 +【疯狂】/【无力】/【蓄锐·增】。以正文为准会让 DM 给错预算。
    """
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[1] / "规则正文.md").read_text(encoding="utf-8")
    line = next(l for l in rules.splitlines() if l.startswith("[速限]（速度上限）："))
    assert "所有角色的出手预算都是基础 2 次" in line
    assert "攻击次数" not in line


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
