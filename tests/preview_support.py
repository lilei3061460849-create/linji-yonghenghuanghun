"""预演/结算相关测试共用的引擎构造：一局「战斗内、轮回者待出手」的确定性对局。

为什么单独一个文件：预演契约测试、沙盒污染测试、结算上下文测试都需要**同一个**
初始状态；分散在各文件里各写一份，任何一处漏改都会让「同种子对照」失去意义。
"""
from __future__ import annotations

from typing import Optional

DEFAULT_DAOWEN = ("庇护", "再生", "冲击", "杀伐", "连击")


def build_battle_engine(tmp_path, name: str = "a", seed: int = 4,
                        region: str = "龙心谷",
                        daowen=DEFAULT_DAOWEN):
    """返回进入「战斗第 1 回合、轮回者待出手」的引擎。

    与 tests/test_action_preview_parity.py 的构造同构：固定属性点、固定遗物选择、
    固定道纹组合，保证同种子两次构造逐字段一致（对照测试的前提）。
    """
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen

    engine = GameEngine(db_path=str(tmp_path / f"{name}.db"),
                        save_dir=str(tmp_path / name),
                        death_book_path=str(tmp_path / f"{name}_book.md"),
                        rng_seed=seed)
    engine.execute_action("setup_attributes",
                          {"name": "贾凡", "blood_points": 11,
                           "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    # 2026-10-12 新增【承载上限】(30X[血限]=X 种道纹)：本构造器的学习循环不检查
    # success，额度不足会被静默吞掉，导致下游报「未持有道纹」而看不出根因。
    # 对照测试要求固定属性点，故不动 11/8/6，只抬高血限让额度覆盖 DEFAULT_DAOWEN。
    engine.state.player.blood_limit = 150
    for dw in daowen:
        engine.execute_action("pre_battle_action",
                              {"sub_action": "学习", "sub": "daowen", "name": dw})
    engine.state.energy = 0
    relic = engine.state.relics[0].name if engine.state.relics else ""
    engine.execute_action("battle_start",
                          {"relic_choices": {relic: {"use": False}} if relic else {}})
    engine.execute_action("round_start", {})
    return engine


def build_pre_battle_engine(tmp_path, name: str = "evt", seed: int = 4,
                            region: str = "龙心谷"):
    """返回停在「局外阶段」的引擎（事件只能在局外结算）。"""
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen

    engine = GameEngine(db_path=str(tmp_path / f"{name}.db"),
                        save_dir=str(tmp_path / name),
                        death_book_path=str(tmp_path / f"{name}_book.md"),
                        rng_seed=seed)
    engine.execute_action("setup_attributes",
                          {"name": "贾凡", "blood_points": 11,
                           "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})

    # 2026-10-12 新增【承载上限】(30X[血限]=X 种道纹)：本文件测的不是额度（额度由 tests/test_load_capacity.py 专测），
    # 夹具 11/8/6 → [血限]66 只有 2 种额度，会把断言淹没在额度报错里，故抬高血限。
    engine.state.player.blood_limit = 150
    return engine
