"""随机 PvP 角色构建与对局组装助手。

这是被测试和 CLI 审计脚本 import 的研究库，按仓库约定保留在 sim/ 根目录；
不被 engine/ 或 webapp/ 生产代码依赖。
"""
from __future__ import annotations

import copy
import random
from pathlib import Path

from engine.api import GameEngine
from engine.enums import GamePhase

ATTRIBUTE_BUDGET = 25
REGION = "龙心谷"
RESONANCES = ("转换", "反转", "曲解")


def legal_attribute_splits() -> list[tuple[int, int, int]]:
    """合法的（血限属性点、速限属性点、法限属性点）分配，总额固定25。"""
    return [
        (blood, speed, mana)
        for blood in range(1, ATTRIBUTE_BUDGET + 1)
        for speed in range(2, ATTRIBUTE_BUDGET - blood + 1, 2)
        for mana in range(2, ATTRIBUTE_BUDGET - blood - speed + 1, 2)
        if blood + speed + mana == ATTRIBUTE_BUDGET
    ]


def _require_success(result: dict, step: str) -> dict:
    if not result.get("success"):
        raise RuntimeError(f"{step}失败：{result}")
    return result


def create_random_character(
    seed: int, name: str, workdir: Path,
    attribute_points: tuple[int, int, int] | None = None,
) -> tuple[GameEngine, dict]:
    """使用公开开局 action 创建角色；随机只负责从合法发现候选中选取。"""
    rng = random.Random(seed)
    blood_points, speed_points, mana_points = (
        attribute_points if attribute_points is not None
        else rng.choice(legal_attribute_splits())
    )
    engine = GameEngine(
        db_path=str(workdir / f"{name}.sqlite"),
        rng_seed=seed,
        sealed_candidate_path=str(workdir / f"{name}-sealed.json"),
        death_book_path=str(workdir / f"{name}-death-book.md"),
    )
    setup = _require_success(engine.execute_action("setup_attributes", {
        "name": name,
        "blood_points": blood_points,
        "speed_points": speed_points,
        "mana_points": mana_points,
    }), f"{name}属性分配")

    relic_choices = list(setup["result"]["relic_choices"])
    if not relic_choices:
        raise RuntimeError(f"{name}开局遗物候选为空")
    relic_name = rng.choice(relic_choices)
    _require_success(engine.execute_action("choose_discovered_relic", {
        "relic_name": relic_name,
    }), f"{name}选择开局遗物")

    daowen_choices = list(engine.state.pending_initial_daowen_choices)
    if not daowen_choices:
        raise RuntimeError(f"{name}初始道纹候选为空")
    daowen_name = rng.choice(daowen_choices)
    _require_success(engine.execute_action("setup_choose_initial_daowen", {
        "daowen_name": daowen_name,
    }), f"{name}选择初始道纹")

    resonance = rng.choice(RESONANCES)
    _require_success(engine.execute_action("setup_choose_resonance", {
        "resonance_type": resonance,
    }), f"{name}选择残韵")
    _require_success(engine.execute_action("setup_choose_region", {
        "region": REGION,
    }), f"{name}选择副本")

    player = engine.state.player
    # 兼容 PvP 双侧残韵读取：挑战者走 State 兼容账本，守擂者走 Entity 独立账本。
    player.resonance = dict(engine.state.resonance)
    build = {
        "name": name,
        "attribute_points": {
            "blood": blood_points,
            "speed": speed_points,
            "mana": mana_points,
            "total": blood_points + speed_points + mana_points,
        },
        "stats": {
            "blood_limit": player.blood_limit,
            "mana_limit": player.mana_limit,
            "speed_limit": player.speed_limit,
            "action_count": player.action_count,
        },
        "relic": relic_name,
        "relic_choices": relic_choices,
        "daowen": daowen_name,
        "daowen_choices": daowen_choices,
        "resonance": resonance,
    }
    return engine, build


def _initiative_key(entity) -> tuple[int, int, int, int]:
    return (entity.speed_limit, entity.mana_limit, entity.blood_limit, entity.current_hp)


def assemble_duel(challenger_engine: GameEngine, defender_engine: GameEngine,
                  seed: int) -> str:
    """把两个独立合法开局角色组装为 PvP 状态；返回先手侧。"""
    challenger = challenger_engine.state.player
    defender = copy.deepcopy(defender_engine.state.player)
    if challenger is None or defender is None:
        raise RuntimeError("随机角色构建不完整")

    defender.name = defender.name or "随机守擂者"
    defender.resonance = dict(defender_engine.state.resonance)
    defender.relics = copy.deepcopy(defender_engine.state.relics)

    state = challenger_engine.state
    state.enemies = [defender]
    state.opponent_relics = copy.deepcopy(defender_engine.state.relics)
    state.in_final_duel = True
    state.phase = GamePhase.IN_COMBAT.value
    state.combat_subphase = "await_round_start"
    state.duel_rounds_started = 0

    left = _initiative_key(challenger)
    right = _initiative_key(defender)
    if left == right:
        first = random.Random(seed ^ 0xD0E1).choice(("player_side", "opponent_side"))
        state.duel_tie_alternating = True
    else:
        first = "player_side" if left > right else "opponent_side"
        state.duel_tie_alternating = False
    state.duel_round_first = first
    state.duel_turn = first
    return first
