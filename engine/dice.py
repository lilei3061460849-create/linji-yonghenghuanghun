"""
随机数引擎
核心规则（2026-08-09 起）：需要随机时，先统计当前池中符合条件的选项总数，
由引擎自身在该范围内生成随机数并直接结算对应选项，过程记录种子与序号以便追溯复现。
玩家与DM不再需要手动提供数字。

历史遗留的手动流程（create_pool 计算范围 → 外部提供数字 → resolve_pool 结算）
仍然保留，供需要人工指定结果的调试/回归测试/DM强制裁定场景使用，
但游戏内实际玩法（探索、共鸣、开局遗物等）一律改为调用 auto_roll。
"""
import hashlib
import random
import time
from typing import Any, Optional


class DiceEngine:
    """随机数引擎 - 池系统；auto_roll 为默认自动结算入口，create_pool/resolve_pool 为手动入口"""
    
    def __init__(self, seed: Optional[int] = None):
        self._pools: dict[str, list] = {}  # 命名池
        self._history: list[dict] = []     # 历史记录（可追溯）
        self.rerolls_pending: int = 0      # 逆命筹：剩余可重投次数（下次 auto_roll 起逐次消耗）
        self._rng = random.Random(seed)    # 引擎自身的随机源；传入seed可复现
        # 世界事件（地图刷新/自主单位）专用随机源：与战斗骰流隔离。
        # 若共用 _rng：随机刷一个冠冕就会挪动后续每一次战斗骰，回放不可比对；
        # 而且 auto_roll 会消耗逆命筹——地图刷新不该花掉玩家的重投。
        self._world_rng = random.Random(f"{seed}:world")
        self._seed = seed
    
    def create_pool(self, pool_name: str, options: list[Any]) -> dict:
        """
        创建随机池
        返回：{pool_name, count, range: "1~N", options_preview}
        AI拿到这个range后，必须向用户索取数字
        """
        if not options:
            raise ValueError(f"池 '{pool_name}' 不能为空")
        
        self._pools[pool_name] = list(options)
        count = len(options)
        
        return {
            "pool_name": pool_name,
            "count": count,
            "range": f"1~{count}",
            "options_preview": [
                {"index": i + 1, "value": str(opt)} 
                for i, opt in enumerate(options)
            ],
            "instruction": f"请在 1~{count} 中选择一个数字（必须由玩家提供，AI禁止自行选择）"
        }
    
    def resolve_pool(self, pool_name: str, player_number: int) -> dict:
        """
        用玩家提供的数字解析池
        返回选定结果
        """
        if pool_name not in self._pools:
            raise ValueError(f"池 '{pool_name}' 不存在")
        
        pool = self._pools[pool_name]
        count = len(pool)
        
        if not 1 <= player_number <= count:
            raise ValueError(f"数字 {player_number} 超出范围 1~{count}")
        
        selected_index = player_number - 1
        selected = pool[selected_index]
        
        # 记录历史
        record = {
            "pool_name": pool_name,
            "player_number": player_number,
            "pool_size": count,
            "selected_index": selected_index,
            "selected_value": str(selected),
            "timestamp": time.time()
        }
        self._history.append(record)
        
        # 从池中移除已选（可选，取决于场景）
        pool.pop(selected_index)
        
        return {
            "pool_name": pool_name,
            "player_number": player_number,
            "selected": selected,
            "remaining_in_pool": len(pool),
            "record": record
        }
    
    def randrange(self, n: int) -> int:
        """委托给引擎自身的随机源，返回 [0, n) 之间的随机整数（供需要rng对象的调用方使用）"""
        return self._rng.randrange(n)

    def world_roll(self, pool_name: str, options: list[Any], context: str = "") -> dict:
        """世界事件专用自动结算：独立骰流、不消耗逆命筹、不扰动战斗骰。

        结果结构与 auto_roll 一致（含 record.selected_index），便于审计与复现。
        """
        if not options:
            raise ValueError(f"池 '{pool_name}' 不能为空")
        pool_info = self.create_pool(pool_name, options)
        number = self._world_rng.randrange(pool_info["count"]) + 1
        result = self.resolve_pool(pool_name, number)
        result["auto"] = True
        result["channel"] = "world"
        result["context"] = context
        result["seed"] = self._seed
        result["rerolled"] = False
        if self._history:
            self._history[-1]["auto"] = True
            self._history[-1]["channel"] = "world"
            self._history[-1]["context"] = context
            self._history[-1]["seed"] = self._seed
        return result

    def set_rerolls(self, count: int) -> None:
        """登记剩余可重投次数（可叠加）。"""
        self.rerolls_pending = max(0, count)

    def auto_roll(self, pool_name: str, options: list[Any], context: str = "") -> dict:
        """
        自动结算入口（默认规则）：统计范围→引擎自身生成随机数→直接结算，全程记录种子与序号。
        返回结构与 resolve_pool 一致，额外附带 auto=True、context、rolled_number、seed。
        逆命筹：若 rerolls_pending>0，本次随机多投一次、
        以第二次结果为准（改变结果），并消耗 1 次重投。
        """
        if not options:
            raise ValueError(f"池 '{pool_name}' 不能为空")
        pool_info = self.create_pool(pool_name, options)
        count = pool_info["count"]
        number = self._rng.randrange(count) + 1  # 复用 resolve_pool 的 1-based 约定
        rerolled = False
        if self.rerolls_pending > 0:
            number = self._rng.randrange(count) + 1
            self.rerolls_pending -= 1
            rerolled = True
        result = self.resolve_pool(pool_name, number)
        result["auto"] = True
        result["context"] = context
        result["seed"] = self._seed
        result["rerolled"] = rerolled
        if self._history:
            self._history[-1]["auto"] = True
            self._history[-1]["context"] = context
            self._history[-1]["seed"] = self._seed
            self._history[-1]["rerolled"] = rerolled
        return result

    def get_pool_status(self, pool_name: str) -> Optional[dict]:
        """获取池状态"""
        if pool_name not in self._pools:
            return None
        pool = self._pools[pool_name]
        return {
            "pool_name": pool_name,
            "count": len(pool),
            "range": f"1~{len(pool)}" if pool else "空池"
        }

    def get_history(self) -> list[dict]:
        """获取所有历史记录"""
        return list(self._history)
