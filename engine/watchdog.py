"""
防死循环与战术诊断守门员系统 (Watchdog & Tactical Diagnostic System)

功能：
1. 战斗时序与动作级停滞探针（CombatWatchdog）：
   - 监控单场战斗时钟与动作：支持 5 分钟（300秒）超时自动中断检查。
   - 监控连续出手无敌方血量削减、伤害被完全吸收或循环出招的情况（连续 N 次无实质伤害）。
   - 触发中断后自动输出精准机制归因（如 固执上限 / 爆裂反噬 / 自愈回满 / 飞行阻断 / 龙鳞减免 / 护盾过厚 / 法力不足等）与对应战术解法（反转/曲解/多段打击/高档修行等）。
2. 连续失败熔断与综合归因（RunFailureWatchdog）：
   - 监控连续多轮轮回未通关情况：连续 10 场轮回失败时自动熔断。
   - 自动分析主瓶颈场次、致命死因分布、经济卡点与道纹构筑缺陷，提供宏观攻坚策略建议。
"""

import time
from typing import Optional, Any, Dict, List
from engine.models import GameState, Entity


class CombatWatchdog:
    """单场战斗时钟与停滞死循环监控器"""

    def __init__(
        self,
        max_stagnant_actions: int = 6,
        max_rounds: int = 25,
        timeout_seconds: float = 300.0,
    ):
        self.max_stagnant_actions = max_stagnant_actions
        self.max_rounds = max_rounds
        self.timeout_seconds = timeout_seconds
        self.history: List[Dict[str, Any]] = []
        self.consecutive_stagnant_actions = 0
        self.last_monster_hp_snapshot: Dict[str, int] = {}
        self.battle_start_time = time.time()
        self.last_progress_time = time.time()

    def reset_battle(self, state: Optional[GameState] = None):
        """重置战斗监控时钟与快照"""
        self.history.clear()
        self.consecutive_stagnant_actions = 0
        self.battle_start_time = time.time()
        self.last_progress_time = time.time()
        if state:
            live = [m for m in state.enemies if m.is_alive]
            self.last_monster_hp_snapshot = {m.name: m.current_hp for m in live}
        else:
            self.last_monster_hp_snapshot.clear()

    def record_action(
        self,
        action_name: str,
        params: dict,
        result: dict,
        state: GameState,
        current_time: Optional[float] = None,
    ) -> Optional[dict]:
        """记录一次出手并检查是否存在超时或停滞死循环。若判定陷入死循环则返回诊断中断字典。"""
        now = current_time if current_time is not None else time.time()

        if not result.get("success"):
            return None

        # 1. 检查 5 分钟（300秒）无进展超时
        elapsed_since_progress = now - self.last_progress_time
        if elapsed_since_progress >= self.timeout_seconds:
            diag = self.diagnose_stagnation(state)
            return {
                "interrupted": True,
                "reason": "timeout_5min",
                "message": f"战斗在 {elapsed_since_progress:.1f} 秒（超过 {self.timeout_seconds} 秒/5分钟）内无实质进展，触发超时自动中断！",
                "elapsed_seconds": elapsed_since_progress,
                "diagnosis": diag,
            }

        # 捕获怪物生命快照
        live_monsters = [m for m in state.enemies if m.is_alive]
        current_snapshot = {m.name: m.current_hp for m in live_monsters}

        # 检查怪物生命是否有净减少
        total_hp_before = sum(self.last_monster_hp_snapshot.values()) if self.last_monster_hp_snapshot else sum(current_snapshot.values())
        total_hp_now = sum(current_snapshot.values())

        is_stagnant = False
        if action_name in ("use_daowen", "attack", "resolve_attack", "resolve_monster_phase"):
            if total_hp_now >= total_hp_before and total_hp_now > 0:
                self.consecutive_stagnant_actions += 1
                is_stagnant = True
            else:
                self.consecutive_stagnant_actions = 0
                self.last_progress_time = now

        self.last_monster_hp_snapshot = current_snapshot
        self.history.append({
            "action": action_name,
            "params": params,
            "result_action": result.get("action"),
            "stagnant": is_stagnant,
            "monster_hp": current_snapshot,
            "player_hp": state.player.current_hp if state.player else 0,
            "round": state.current_round,
            "timestamp": now,
        })

        # 2. 检查连续动作无伤害停滞
        if self.consecutive_stagnant_actions >= self.max_stagnant_actions:
            diag = self.diagnose_stagnation(state)
            return {
                "interrupted": True,
                "reason": "stagnant_loop",
                "message": f"检测到连续 {self.consecutive_stagnant_actions} 次出手敌方总生命未出现削减，触发防死循环自动中断！",
                "diagnosis": diag,
            }

        return None

    def diagnose_stagnation(self, state: GameState) -> dict:
        """分析导致停滞的怪物机制与应对方案"""
        findings = []
        recommendations = []
        player = state.player

        for m in state.enemies:
            if not m.is_alive:
                continue

            # 1. 固执判定
            if m.has_status("固执"):
                val = m.get_status_value("固执")
                findings.append(f"敌方【{m.name}】处于【固执{val}】状态（单次失去生命上限为1），常规大额伤害被完全锁死。")
                recommendations.append(f"对策方案：使用【残韵·反转】将【固执】逆转为【连击】，或使用【连击X】打出5X次独立1点伤害绕过上限！")

            # 2. 飞行判定（2026-10-03：【坠落】道纹删除，击落手段移除）
            if m.has_status("飞行") or m.is_flying:
                findings.append(f"敌方【{m.name}】处于【飞行】状态，地面攻击无法锁定目标。")
                recommendations.append("对策方案：使用扭曲工具【反怪物电击枪】对其造成额外伤害，或用【残韵】篡改其【飞行】状态！")

            # 4. 自愈回血抵消判定
            if m.has_status("自愈"):
                findings.append(f"敌方【{m.name}】处于【自愈】状态，回始巨额回复抵消了常规攻击。")
                recommendations.append(f"对策方案：使用【残韵·反转】将【自愈】篡改为【衰败】使其自损生命！")

            # 5. 格挡阻隔判定
            if m.shield >= 20:
                findings.append(f"敌方【{m.name}】持有高额格挡（{m.shield}点），低额攻击无法破防。")
                recommendations.append(f"对策方案：使用【贯穿】（无视格挡）或高代数【杀伐】破盾！")

            # 6. 龙鳞减伤判定
            if m.has_status("龙鳞"):
                val = m.get_status_value("龙鳞")
                findings.append(f"敌方【{m.name}】处于【龙鳞{val}】状态，每次受到伤害减少 {val} 点。")
                recommendations.append(f"对策方案：使用【残韵·曲解】逆转龙鳞，或使用单发极高爆发直接破除减伤。")

        if not findings:
            findings.append("施法者法力或代数配置不足，未能击穿敌方护盾或产生足够伤害。")
            recommendations.append("提高局外修行档位，将法限提升至40~60以上，增强单次出手爆发力。")

        return {
            "findings": findings,
            "recommendations": recommendations,
        }


class RunFailureWatchdog:
    """连续失败熔断与综合归因分析器"""

    def __init__(self, max_consecutive_failures: int = 10):
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self.failure_records: List[Dict[str, Any]] = []

    def record_run(self, cleared_battles: int, won: bool, death_cause: str, details: dict = None) -> Optional[dict]:
        """记录一次轮回通关尝试"""
        if won:
            self.consecutive_failures = 0
            return None

        self.consecutive_failures += 1
        self.failure_records.append({
            "run_index": len(self.failure_records) + 1,
            "cleared_battles": cleared_battles,
            "death_cause": death_cause,
            "details": details or {},
        })

        if self.consecutive_failures >= self.max_consecutive_failures:
            return {
                "interrupted": True,
                "reason": "max_failures_exceeded",
                "message": f"连续 {self.consecutive_failures} 轮轮回未能通关，触发熔断保护并停止盲目重试！",
                "analysis": self.analyze_failures(),
            }
        return None

    def analyze_failures(self) -> dict:
        """分析连续失败的宏观结构性原因"""
        battle_stops = {}
        for r in self.failure_records[-self.max_consecutive_failures:]:
            b = r["cleared_battles"] + 1
            battle_stops[b] = battle_stops.get(b, 0) + 1

        common_stopper = max(battle_stops.items(), key=lambda x: x[1])[0]
        return {
            "total_failures": self.consecutive_failures,
            "failure_distribution": battle_stops,
            "primary_bottleneck_battle": f"第 {common_stopper} 场",
            "suggestion": "检查该场次怪物的专属道纹机制，并在前置局外阶段准备对应残韵（反转/曲解/转换）与高档位修行法力。",
        }
