"""
规则校验器
核心职责：
1. 每次行动后自动检查是否符合规则
2. 发现违规时记录详细上下文，等待DM裁定
3. DM可以选择：修复（回退违规操作）或转正（记录为特例/惯例）
"""
from __future__ import annotations
import sqlite3
import json
import time
import os
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Optional, Any, Callable
from .models import Entity, GameState, StatusEffect
from .enums import InterruptType


# ========== 机制系统护栏（MVP，见审计报告 Phase 0） ==========
# 只针对"已经迁移到 Mechanism Registry 的机制"：核心管线文件里禁止再次出现
# 同名机制的硬编码 has_status 分支。不扫描/不禁止历史代码，机制声明层
# （engine/mechanisms/）自身也不在护栏范围内。

_COMBAT_GUARD_ROOT = Path(__file__).resolve().parent


def _combat_guard_files() -> tuple:
    """护栏扫描范围：combat 门面 + 全部分片 + 其余核心管线文件。

    拆分（见 engine/combat_parts/）后，同一批方法体分散在多个文件里，
    若只扫门面会把护栏悄悄失效——新分片必须自动纳入本元组。
    """
    shards = tuple(sorted(
        str(p.relative_to(_COMBAT_GUARD_ROOT.parent))
        for p in (_COMBAT_GUARD_ROOT / "combat_parts").glob("*.py")
    ))
    return ("engine/combat.py",) + shards + ("engine/combat_hooks.py", "engine/api.py")


_MIGRATION_GUARD_PROTECTED_FILES = _combat_guard_files()


def _mechanism_guard_scan(protected_files: tuple) -> list[dict]:
    from .mechanisms import MECHANISMS

    root = Path(__file__).resolve().parent.parent
    violations: list[dict] = []
    names = MECHANISMS.names()
    for rel in protected_files:
        text = (root / rel).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for name in names:
                if f'has_status("{name}")' in line or f"has_status('{name}')" in line:
                    violations.append({
                        "severity": "error",
                        "rule_name": "已迁移机制护栏",
                        "rule_text": f"【{name}】已迁移到机制声明层，核心管线禁止新增同名硬编码分支",
                        "violation_description": f"{rel}:{line_no}: {stripped}",
                        "context": {"file": rel, "line": line_no, "mechanism": name},
                    })
    return violations


@lru_cache(maxsize=4)
def _mechanism_guard_scan_cached(protected_files: tuple) -> tuple:
    return tuple(_mechanism_guard_scan(protected_files))


def check_migrated_mechanism_guards(
    protected_files: Optional[list[str]] = None,
) -> list[dict]:
    """检查核心管线是否重新引入了已迁移机制的硬编码分支。返回违规列表（不抛异常）。"""
    files = tuple(protected_files) if protected_files else _MIGRATION_GUARD_PROTECTED_FILES
    return list(_mechanism_guard_scan_cached(files))


class ViolationSeverity:
    """违规严重程度"""
    INFO = "info"           # 仅提醒，不影响游戏
    WARNING = "warning"     # 警告，可能影响平衡
    ERROR = "error"         # 严重违规，必须处理
    CRITICAL = "critical"   # 致命错误，必须回退


class RuleViolation:
    """违规记录"""
    
    def __init__(
        self,
        violation_id: int = 0,
        severity: str = "warning",
        rule_name: str = "",
        rule_text: str = "",
        violation_description: str = "",
        context: dict = None,
        action: dict = None,
        state_snapshot: dict = None,
        dm_decision: str = "",       # "fix" / "legitimize" / "ignore"
        dm_note: str = "",
        resolved: bool = False,
        created_at: float = 0
    ):
        self.violation_id = violation_id
        self.severity = severity
        self.rule_name = rule_name
        self.rule_text = rule_text
        self.violation_description = violation_description
        self.context = context or {}
        self.action = action or {}
        self.state_snapshot = state_snapshot or {}
        self.dm_decision = dm_decision
        self.dm_note = dm_note
        self.resolved = resolved
        self.created_at = created_at or time.time()
    
    def to_dict(self) -> dict:
        return {
            "violation_id": self.violation_id,
            "severity": self.severity,
            "rule_name": self.rule_name,
            "rule_text": self.rule_text,
            "violation_description": self.violation_description,
            "context": self.context,
            "action": self.action,
            "state_snapshot": self.state_snapshot,
            "dm_decision": self.dm_decision,
            "dm_note": self.dm_note,
            "resolved": self.resolved,
            "created_at": self.created_at,
        }


class RuleValidator:
    """
    规则校验器
    对每个行动结果进行多层次校验
    """
    
    def __init__(self, db_path: str = "data/violations.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self._checks: list[Callable] = []
        self._register_builtin_checks()
        # 机制系统护栏（静态检查，只报告不改行为）：已迁移机制不得在核心管线里
        # 重新出现同名硬编码 has_status 分支。结果供测试/审计读取。
        self.migration_guard_violations = check_migrated_mechanism_guards()
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                severity TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                rule_text TEXT DEFAULT '',
                violation_description TEXT NOT NULL,
                context_json TEXT DEFAULT '{}',
                action_json TEXT DEFAULT '{}',
                state_json TEXT DEFAULT '{}',
                dm_decision TEXT DEFAULT '',
                dm_note TEXT DEFAULT '',
                resolved INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rule_exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_name TEXT NOT NULL,
                exception_key TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                data_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    
    def _register_builtin_checks(self):
        """注册内置检查规则"""
        self._checks = [
            self._check_hp_bounds,
            self._check_mana_bounds,
            self._check_speed_bounds,
            self._check_shield_non_negative,
            self._check_daowen_not_used_without_holding,
            self._check_cost_mana_sufficient,
            self._check_cost_hp_survival,
            self._check_attribute_points_sum,
            self._check_action_count_valid,
            self._check_damage_non_negative,
            self._check_heal_non_negative,
            self._check_cooldown_valid,
            self._check_frozen_daowen_blocked,
            self._check_flying_targeting,
            self._check_dodge_speed_cost,
            self._check_shard_non_negative,
            self._check_resonance_available,
            self._check_energy_non_negative,
            self._check_monster_difficulty_signals,
            self._check_unseen_rule_consistency,
        ]
    
    # ========== 主校验入口 ==========
    
    def validate(
        self, 
        state: GameState, 
        action: dict, 
        result: dict
    ) -> dict:
        """
        校验一次行动结果
        返回：{valid, violations, warnings, auto_fixes}
        """
        violations = []
        warnings = []
        
        for check_func in self._checks:
            try:
                check_result = check_func(state, action, result)
                if check_result:
                    if check_result["severity"] in ("error", "critical"):
                        violations.append(check_result)
                    else:
                        warnings.append(check_result)
            except Exception as e:
                # 校验器自身出错也要记录
                warnings.append({
                    "severity": "info",
                    "rule_name": "校验器内部错误",
                    "violation_description": f"检查函数 {check_func.__name__} 出错: {str(e)}",
                })
        
        # 保存违规记录
        all_issues = violations + warnings
        for issue in all_issues:
            v = RuleViolation(
                severity=issue.get("severity", "warning"),
                rule_name=issue.get("rule_name", ""),
                rule_text=issue.get("rule_text", ""),
                violation_description=issue.get("violation_description", ""),
                context=issue.get("context", {}),
                action=action,
                state_snapshot=state.to_dict() if state else {}
            )
            self._save_violation(v)
        
        return {
            "valid": len(violations) == 0,
            "violations_count": len(violations),
            "warnings_count": len(warnings),
            "violations": [v for v in violations],
            "warnings": [w for w in warnings],
            "instruction": (
                "存在严重违规，需要DM处理" if violations else
                f"校验通过（{len(warnings)}条提醒）" if warnings else
                "校验完全通过"
            )
        }
    
    # ========== 内置检查规则 ==========
    
    def _check_hp_bounds(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查生命值边界"""
        for entity in self._get_all_entities(state):
            if entity.current_hp < 0:
                return {
                    "severity": "error",
                    "rule_name": "生命值下限",
                    "rule_text": "当前生命不能为负数",
                    "violation_description": f"{entity.name} 当前生命为 {entity.current_hp}（负数）",
                    "context": {"entity": entity.name, "hp": entity.current_hp}
                }
            if entity.current_hp > entity.blood_limit:
                return {
                    "severity": "warning",
                    "rule_name": "生命值上限",
                    "rule_text": "当前生命不能超过血限",
                    "violation_description": f"{entity.name} 当前生命 {entity.current_hp} > 血限 {entity.blood_limit}",
                    "context": {"entity": entity.name, "hp": entity.current_hp, "limit": entity.blood_limit}
                }
        return None
    
    def _check_mana_bounds(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查法力边界"""
        if state.player:
            if state.player.current_mana < 0:
                return {
                    "severity": "error",
                    "rule_name": "法力下限",
                    "rule_text": "法力不能为负数",
                    "violation_description": f"{state.player.name} 法力为 {state.player.current_mana}",
                    "context": {"mana": state.player.current_mana}
                }
            if state.player.current_mana > state.player.mana_limit * 2:
                return {
                    "severity": "warning",
                    "rule_name": "法力异常高",
                    "rule_text": "法力超过法限2倍可能异常",
                    "violation_description": f"法力 {state.player.current_mana}，法限 {state.player.mana_limit}",
                    "context": {"mana": state.player.current_mana, "limit": state.player.mana_limit}
                }
        return None
    
    def _check_speed_bounds(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查速度边界"""
        if state.player and state.player.current_speed < 0:
            return {
                "severity": "error",
                "rule_name": "速度下限",
                "rule_text": "当前速度不能为负数",
                "violation_description": f"{state.player.name} 当前速度为 {state.player.current_speed}",
                "context": {"speed": state.player.current_speed}
            }
        return None
    
    def _check_shield_non_negative(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查格挡非负"""
        for entity in self._get_all_entities(state):
            if entity.shield < 0:
                return {
                    "severity": "error",
                    "rule_name": "格挡非负",
                    "rule_text": "格挡值不能为负数",
                    "violation_description": f"{entity.name} 格挡为 {entity.shield}",
                    "context": {"entity": entity.name, "shield": entity.shield}
                }
        return None
    
    def _check_daowen_not_used_without_holding(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查是否使用了未持有的道纹"""
        if action.get("action") == "use_daowen":
            name = action.get("params", {}).get("daowen_name", "")
            if state.player and name and name not in state.player.dao_wen:
                # 检查是否是特例
                if not self._is_exception("未持有道纹", f"use_{name}"):
                    return {
                        "severity": "critical",
                        "rule_name": "禁止发动未持有道纹",
                        "rule_text": "禁止发动未持有的道纹、未定义的法术",
                        "violation_description": f"尝试发动未持有的道纹【{name}】",
                        "context": {"daowen": name, "held": list(state.player.dao_wen.keys())}
                    }
        return None
    
    def _check_cost_mana_sufficient(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查法力是否足够支付消耗"""
        if state.player and result.get("action", "").startswith("发动道纹"):
            calc = result.get("calculation", {})
            if calc.get("cost_type") == "消耗":
                cost = calc.get("cost", 0)
                # 回溯检查：消耗前法力是否足够
                # 这个检查需要在行动前状态，这里只能检查结果状态
                pass
        return None
    
    def _check_cost_hp_survival(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查支付代价后是否存活（代价致死需要特殊处理）"""
        execution = result.get("execution", {})
        effects = execution.get("effects", [])
        for effect in effects:
            if effect.get("type") == "bleed_cost" and effect.get("died"):
                return {
                    "severity": "warning",
                    "rule_name": "代价致死",
                    "rule_text": "生命支付导致死亡，需要确认是否有龙心等抵消手段",
                    "violation_description": f"支付生命后死亡",
                    "context": {"effect": effect}
                }
        return None
    
    def _check_attribute_points_sum(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查属性点分配总和"""
        if action.get("action") == "setup_attributes":
            params = action.get("params", {})
            total = params.get("blood_points", 0) + params.get("speed_points", 0) + params.get("mana_points", 0)
            if total > 25:
                return {
                    "severity": "error",
                    "rule_name": "属性点总和",
                    "rule_text": "初始属性点总和不能超过25（余点存入属性点池）",
                    "violation_description": f"分配总和为{total}，不得超过25",
                    "context": {"total": total}
                }
        return None
    
    def _check_action_count_valid(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查出手次数是否合理"""
        if state.player:
            # 轮回者主动出手次数与速限分离；普攻本身不支付当前速度。
            expected = max(0, 2 + state.player.get_status_value("疯狂")
                          - state.player.get_status_value("无力"))
            actual = state.player.action_count
            if actual != expected:
                return {
                    "severity": "warning",
                    "rule_name": "出手次数计算",
                    "rule_text": "出手按动作槽位记账：action_count = 基础2次 +【疯狂】/【无力】/【蓄锐·增】修正",
                    "violation_description": f"实际出手预算{actual}，规则计算应为{expected}",
                    "context": {"action_count": actual, "expected": expected}
                }
        return None
    
    def _check_damage_non_negative(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查伤害非负"""
        execution = result.get("execution", {})
        effects = execution.get("effects", [])
        for effect in effects:
            if effect.get("type") == "damage" and effect.get("actual_damage", 0) < 0:
                return {
                    "severity": "error",
                    "rule_name": "伤害非负",
                    "rule_text": "伤害值不能为负数",
                    "violation_description": f"计算出负伤害: {effect.get('actual_damage')}",
                    "context": {"effect": effect}
                }
        return None
    
    def _check_heal_non_negative(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查回复非负"""
        execution = result.get("execution", {})
        effects = execution.get("effects", [])
        for effect in effects:
            if effect.get("type") == "heal" and effect.get("actual_heal", 0) < 0:
                return {
                    "severity": "error",
                    "rule_name": "回复非负",
                    "rule_text": "回复值不能为负数",
                    "violation_description": f"计算出负回复: {effect.get('actual_heal')}",
                    "context": {"effect": effect}
                }
        return None
    
    def _check_cooldown_valid(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查冷却状态"""
        if action.get("action") == "use_daowen":
            name = action.get("params", {}).get("daowen_name", "")
            if state.player and name in state.player.dao_wen:
                dw = state.player.dao_wen[name]
                if dw.cooldown_remaining > 0:
                    return {
                        "severity": "error",
                        "rule_name": "冷却中",
                        "rule_text": "冷却中的道纹无法发动",
                        "violation_description": f"【{name}】冷却剩余{dw.cooldown_remaining}场",
                        "context": {"daowen": name, "cooldown": dw.cooldown_remaining}
                    }
        return None
    
    def _check_frozen_daowen_blocked(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查封印状态"""
        if action.get("action") == "use_daowen":
            name = action.get("params", {}).get("daowen_name", "")
            if state.player and name in state.player.dao_wen:
                dw = state.player.dao_wen[name]
                if dw.is_frozen or getattr(dw, "ice_frozen", False):
                    return {
                        "severity": "error",
                        "rule_name": "道纹封印",
                        "rule_text": "被封印的道纹无法发动",
                        "violation_description": f"【{name}】被封印",
                        "context": {"daowen": name}
                    }
        return None
    
    def _check_flying_targeting(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查飞行状态下的目标选择"""
        # 飞行角色无法被非飞行角色选为目标
        execution = result.get("execution", {})
        effects = execution.get("effects", [])
        for effect in effects:
            if effect.get("type") == "damage":
                target_name = effect.get("target", "")
                # 查找目标
                for e in state.enemies + ([state.player] if state.player else []):
                    if e and e.name == target_name and e.is_flying:
                        # 检查攻击者是否有飞行
                        attacker_name = result.get("action", "").split("【")[0] if "【" in result.get("action", "") else ""
                        if not self._is_exception("飞行免疫", f"attack_{target_name}"):
                            return {
                                "severity": "warning",
                                "rule_name": "飞行目标免疫",
                                "rule_text": "无法被非飞行角色选为目标",
                                "violation_description": f"对飞行中的{target_name}发动了攻击",
                                "context": {"target": target_name, "flying": True}
                            }
        return None
    
    def _check_dodge_speed_cost(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查闪避速度消耗"""
        if action.get("action") == "dodge_decision":
            params = action.get("params", {})
            if params.get("dodge"):
                target_name = params.get("target", "")
                for e in self._get_all_entities(state):
                    if e.name == target_name:
                        if e.current_speed < 0:
                            return {
                                "severity": "error",
                                "rule_name": "闪避速度溢出",
                                "rule_text": "闪避消耗1点速度，速度不能为负",
                                "violation_description": f"{target_name} 速度变为 {e.current_speed}",
                                "context": {"entity": target_name, "speed": e.current_speed}
                            }
        return None
    
    def _check_shard_non_negative(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查碎片非负（允许负债的情况除外）"""
        if state.shards < -50:
            return {
                "severity": "error",
                "rule_name": "碎片负债上限",
                "rule_text": "碎片负债不超过50",
                "violation_description": f"碎片为{state.shards}，低于-50下限",
                "context": {"shards": state.shards}
            }
        return None
    
    def _check_resonance_available(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查残韵是否可用"""
        if action.get("action") == "use_resonance":
            rtype = action.get("params", {}).get("resonance_type", "")
            if state.resonance.get(rtype, 0) <= 0:
                if not self._is_exception("残韵", f"use_{rtype}"):
                    return {
                        "severity": "error",
                        "rule_name": "残韵不足",
                        "rule_text": "没有可用的残韵无法使用",
                        "violation_description": f"尝试使用{rtype}残韵，但数量为0",
                        "context": {"type": rtype, "available": state.resonance}
                    }
        return None
    
    def _check_energy_non_negative(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查精力非负"""
        if state.energy < 0:
            return {
                "severity": "error",
                "rule_name": "精力下限",
                "rule_text": "精力不能为负数",
                "violation_description": f"精力为{state.energy}",
                "context": {"energy": state.energy}
            }
        return None
    
    def _check_monster_difficulty_signals(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查怪物是否应触发困境检查"""
        for monster in state.enemies:
            if not monster.is_alive:
                continue
            hp_ratio = monster.hp_ratio
            if hp_ratio <= 0.15 and hp_ratio > 0:
                # 生命极低但没有触发困境
                if not self._is_exception("困境豁免", monster.name):
                    return {
                        "severity": "info",
                        "rule_name": "怪物困境提醒",
                        "rule_text": "怪物陷入困境时应检查是否逃跑/进化",
                        "violation_description": f"{monster.name} 生命仅剩 {hp_ratio*100:.0f}%，可能应触发困境检查",
                        "context": {"monster": monster.name, "hp_ratio": round(hp_ratio, 2)}
                    }
        return None
    
    def _check_unseen_rule_consistency(self, state: GameState, action: dict, result: dict) -> Optional[dict]:
        """检查未见场景是否与现有规则冲突"""
        # 这个检查比较复杂，主要看DM裁定时是否有冲突提示
        return None
    
    # ========== 辅助方法 ==========
    
    def _get_all_entities(self, state: GameState) -> list:
        entities = []
        if state.player:
            entities.append(state.player)
        entities.extend(state.friends)
        entities.extend(state.employees)
        entities.extend(state.temp_friends)
        entities.extend(state.enemies)
        return entities
    
    def _save_violation(self, violation: RuleViolation) -> int:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """INSERT INTO violations 
               (severity, rule_name, rule_text, violation_description, context_json, action_json, state_json, dm_decision, dm_note, resolved, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                violation.severity,
                violation.rule_name,
                violation.rule_text,
                violation.violation_description,
                json.dumps(violation.context, ensure_ascii=False),
                json.dumps(violation.action, ensure_ascii=False),
                json.dumps(violation.state_snapshot, ensure_ascii=False),
                violation.dm_decision,
                violation.dm_note,
                1 if violation.resolved else 0,
                violation.created_at
            )
        )
        vid = cursor.lastrowid
        conn.commit()
        conn.close()
        return vid
    
    # ========== 特例管理 ==========
    
    def _is_exception(self, rule_name: str, key: str) -> bool:
        """检查是否是已注册的特例"""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT id FROM rule_exceptions WHERE rule_name = ? AND exception_key = ?",
            (rule_name, key)
        ).fetchone()
        conn.close()
        return row is not None
    
    def add_exception(self, rule_name: str, key: str, description: str, data: dict = None) -> int:
        """DM注册一个特例（转正为惯例）"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT OR REPLACE INTO rule_exceptions 
                   (rule_name, exception_key, description, data_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (rule_name, key, description, json.dumps(data or {}, ensure_ascii=False), time.time())
            )
            eid = cursor.lastrowid
            conn.commit()
            conn.close()
            return eid
        except sqlite3.IntegrityError:
            conn.close()
            return -1

    def get_exceptions(self, rule_name: str = None) -> list[dict]:
        """获取所有特例"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if rule_name:
            rows = conn.execute(
                "SELECT * FROM rule_exceptions WHERE rule_name = ?", (rule_name,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rule_exceptions").fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    # ========== DM处理接口 ==========
    
    def resolve_violation(self, violation_id: int, decision: str, note: str = "") -> dict:
        """
        DM处理违规
        decision: "fix"（修复） / "legitimize"（转正为惯例） / "ignore"（忽略）
        """
        conn =sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE violations SET dm_decision = ?, dm_note = ?, resolved = 1 WHERE id = ?",
            (decision, note, violation_id)
        )
        conn.commit()
        
        # 获取违规详情
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM violations WHERE id = ?", (violation_id,)).fetchone()
        conn.close()
        
        if not row:
            return {"success": False, "error": f"违规记录 {violation_id} 不存在"}
        
        violation_data = dict(row)
        context = json.loads(violation_data.get("context_json", "{}"))
        
        result = {
            "success": True,
            "violation_id": violation_id,
            "decision": decision,
            "note": note
        }
        
        if decision == "legitimize":
            # 转正为特例
            rule_name = violation_data["rule_name"]
            exception_key = context.get("entity", context.get("daowen", str(violation_id)))
            self.add_exception(
                rule_name, 
                exception_key, 
                f"DM裁定转正: {note}",
                context
            )
            result["action"] = "已转正为特例"
            result["exception_key"] = exception_key
        
        return result
    
    def get_pending_violations(self) -> list[dict]:
        """获取未处理的违规"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM violations WHERE resolved = 0 ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_violation_stats(self) -> dict:
        """违规统计"""
        conn = sqlite3.connect(self.db_path)
        
        total = conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM violations WHERE resolved = 1").fetchone()[0]
        pending = total - resolved
        
        by_severity = {}
        for row in conn.execute("SELECT severity, COUNT(*) as cnt FROM violations GROUP BY severity"):
            by_severity[row[0]] = row[1]
        
        by_decision = {}
        for row in conn.execute("SELECT dm_decision, COUNT(*) as cnt FROM violations WHERE resolved = 1 GROUP BY dm_decision"):
            by_decision[row[0]] = row[1]
        
        exceptions_count = conn.execute("SELECT COUNT(*) FROM rule_exceptions").fetchone()[0]
        
        conn.close()
        
        return {
            "total": total,
            "resolved": resolved,
            "pending": pending,
            "by_severity": by_severity,
            "by_decision": by_decision,
            "exceptions_count": exceptions_count
        }
