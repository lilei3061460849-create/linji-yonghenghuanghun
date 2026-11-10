"""[战终]耐久恢复契约测试（2026-11-09 用户令：取消消耗品通用关键词【自愈X】）。

历史：2026-10-14 曾把消耗品被动收成通用关键词【自愈X】（恢复X点耐久，
可挂「战终／目标死亡时」两个通用时机）。用户 2026-11-09 指出该机制此前已令删除、
不得复活——引擎里那套通用框架已整体移除。

现在只剩「物品正文逐条写明」的那一条：
- 【无限肉块】[战终]恢复80%已损耐久，向上取整；
- 其它消耗品没有这条声明，战终不会被顺手补耐久；
- 「目标死亡时」这个通用时机不存在，玩家出手选中集合也不再记录。
"""
from engine.api import GameEngine
from engine.consumables import DURABILITY_RECOVERY, apply_battle_end_durability_recovery
from engine.models import Consumable

from tests.setup_support import begin_battle, finish_initial_daowen


def _engine(tmp_path) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "rulings.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=19,
    )


def _open(engine):
    """走完开局三步（残韵/副本/遗物），phase 到局外，可 begin_battle。"""
    engine.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    if (setup.get("result") or {}).get("relic_choices"):
        engine.execute_action("choose_discovered_relic",
                              {"relic_name": setup["result"]["relic_choices"][0]})


# ---------------- 事实源：只有一条声明，且没有通用关键词 ----------------

def test_only_infinite_meat_declares_battle_end_recovery():
    assert DURABILITY_RECOVERY == {"infinite_meat": ("percent_lost", 80)}


def test_generic_ziyu_keyword_is_gone():
    """通用关键词与它的两个时机都不得复活（导入就该失败）。"""
    import engine.consumables as mod
    for gone in ("ZIYU_TRIGGER_BATTLE_END", "ZIYU_TRIGGER_TARGET_DEATH",
                 "CONSUMABLE_ZIYU_TRIGGERS", "apply_zi_yu_trigger",
                 "note_action_targets"):
        assert not hasattr(mod, gone), f"消耗品【自愈X】框架不得复活：{gone}"


def test_state_no_longer_tracks_selected_targets():
    """「目标死亡时」没了，选中集合也不该再存在。"""
    from engine.models import GameState
    assert not hasattr(GameState(), "player_selected_targets")


# ---------------- 正常路径：80% 已损耐久，向上取整 ----------------

def test_meat_recovers_80_percent_of_lost_durability():
    meat = Consumable(name="无限肉块", effect="", current_uses=1, max_uses=1,
                      kind="infinite_meat")
    meat.max_uses = meat.current_uses = 0
    meat.max_uses = 5
    meat.current_uses = 0
    logs = apply_battle_end_durability_recovery([meat])
    assert logs[0]["lost"] == 5
    assert logs[0]["restored"] == 4          # ceil(5*0.8)=4
    assert meat.current_uses == 4


def test_meat_rounds_up():
    meat = Consumable(name="无限肉块", effect="", current_uses=0, max_uses=4,
                      kind="infinite_meat")
    logs = apply_battle_end_durability_recovery([meat])
    assert logs[0]["restored"] == 4          # ceil(4*0.8)=4（向上取整）


def test_full_durability_logs_zero_restore():
    meat = Consumable(name="无限肉块", effect="", current_uses=3, max_uses=3,
                      kind="infinite_meat")
    logs = apply_battle_end_durability_recovery([meat])
    assert logs[0]["restored"] == 0 and meat.current_uses == 3


# ---------------- 边界：其它消耗品不被顺手补耐久 ----------------

def test_other_consumables_are_untouched():
    """没有「自愈X」通用关键词之后，别的物品战终不会自己回耐久。"""
    other = Consumable(name="普通护符", effect="战终恢复2", current_uses=0, max_uses=5,
                       kind="charm")
    logs = apply_battle_end_durability_recovery([other])
    assert logs == []
    assert other.current_uses == 0


def test_empty_list_is_fine():
    assert apply_battle_end_durability_recovery([]) == []
    assert apply_battle_end_durability_recovery(None) == []


# ---------------- 引擎落地：战终真的结算 ----------------

def test_battle_end_applies_recovery_through_engine(tmp_path):
    engine = _engine(tmp_path)
    engine.execute_action("setup_attributes", {
        "name": "肉块持有者", "blood_points": 9, "speed_points": 8, "mana_points": 8})
    finish_initial_daowen(engine)
    _open(engine)
    while engine.state.energy > 0:
        engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
    assert begin_battle(engine)["success"]

    meat = Consumable(name="无限肉块", effect="使用后恢复10生命；战终恢复80%已损耐久",
                      current_uses=1, max_uses=3, kind="infinite_meat")
    engine.state.consumables.append(meat)
    engine.state.enemies[:] = []
    engine.state.combat_subphase = "await_round_end"
    done = engine.execute_action("battle_end", {})
    assert done["success"], done
    assert meat.current_uses == 3, "战终应补回 ceil(2*0.8)=2 点"
