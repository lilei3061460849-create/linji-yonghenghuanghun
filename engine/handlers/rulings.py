"""事件与裁定行动：事件结算、读死者之书。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations



from ..action_registry import ACTION_REGISTRY
from ..dm_rulings import Interrupt
from ..enums import InterruptType
from ..events import TWISTED_TOOL_LIBRARY


class RulingHandlersMixin:
    """宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("resolve_event", group="rulings")
    def _action_resolve_event(self, params: dict) -> dict:
        """结算事件选项：自动应用常见代价/收益，特殊效果交DM"""
        from ..events import resolve_option_effect
        name = params.get("event", "")
        option_id = params.get("option_id")
        if self.event_pool.current is None:
            return {"success": False, "error": "当前没有待结算事件；请先通过探索触发事件"}
        if name != self.event_pool.current:
            return {"success": False,
                    "error": f"只能结算当前事件【{self.event_pool.current}】，不能结算【{name}】"}
        ev = self.event_pool.events.get(name)
        if not ev:
            return {"success": False, "error": f"未知事件: {name}"}
        opt = next((o for o in ev["options"] if o["id"] == option_id), None)
        if opt is None:
            return {"success": False, "error": f"事件{name}无选项{option_id}"}
        # 「拒绝改造」等带代价的选项也含「拒绝」，不能当拒绝类。
        # 真拒绝：正文写「无事发生」，或选项以「拒绝：/拒绝:」起头。
        # 真拒绝必须在结算选项效果之前判定（旧版曾据此索取分配去向）。
        text = opt["text"]
        is_reject = self._is_reject_option_text(text)
        has_wusuoqiu = any(r.name == "无所求" and getattr(r, "enabled", True) for r in self.state.relics)
        res = resolve_option_effect(opt["text"], self, event_name=name, params=params)
        if res.get("interrupt_required"):
            interrupt = Interrupt(
                interrupt_type=InterruptType.UNSEEN_SCENE,
                context=res["interrupt_required"],
                description=f"事件【{name}】包含创造性效果，等待DM裁定",
                options=[], state_snapshot=self.state.to_dict(),
            )
            self._pending_interrupts.append(interrupt)
            return {"success": True, "action": f"事件【{name}】等待裁定",
                    "completed": False, "interrupt": interrupt.to_dict()}
        if res.get("error"):
            return {"success": False, "error": res["error"],
                    "pages": res.get("pages"), "instruction": res.get("instruction", "")}
        # 任意事件造成的真实碎片余额变化都立即检查动态还债线；历史峰值只在余额刷新纪录时上升。
        player = self.state.player
        peak = max(0, int(getattr(self.state, "shards_peak", 0)))
        if player and player.is_alive and self.state.shards <= -(10 + peak):
            player.current_hp = 0
            player.depart_battle("还债")
            res.setdefault("applied", []).append(
                f"{player.name}被祭坛嚼碎：碎片余额{self.state.shards}触及动态还债线-{10 + peak}")
            res["debt_result"] = {"entity": player.name, "balance": self.state.shards,
                                  "threshold": 10 + peak, "triggered": True}
        if is_reject:
            # DM 裁定 2026-09-10（用户）：事件选拒绝选项后随机获得一种残韵——
            # 「用其他道纹解决」怪物墙：放弃事件收益换构筑深度，而非数值对撞。
            # 口径复用 _is_reject_option_text（带代价的「拒绝改造」类不算）；
            # 随机走引擎 dice（seed 可复现），入 State.resonance（局中插队可耗）。
            _rtype = ("转换", "反转", "曲解")[self.dice.randrange(3)]
            self.state.resonance[_rtype] = self.state.resonance.get(_rtype, 0) + 1
            res["applied"].append(f"拒绝奖励：随机获得{_rtype}残韵")
        if is_reject and has_wusuoqiu:
            # 2026-09-16 裁定（清单 A1）：【无所求】按《物品索引》原文"永久获得
            # 1点属性点"入属性点池，不再由引擎折算成面板——旧实现按
            # "1属性点=1速限=2法限"直接加速限/法限，与正文
            # 「2属性点=1[速限]=1[法限]=12[血限]」相悖，且把速限与法限做成
            # 不等价。入池后由玩家按正文自行兑换（属性点可存储、随时兑换）。
            self.state.attribute_points += 1
            res["applied"].append(
                f"无所求：+1属性点（属性点池 {self.state.attribute_points}；"
                "按正文 1点=1速限、2点=1法限、1点=6血限，兑换仅限战前）")
        # 过路商人交易是一个持续会话：同次遭遇可连续买卖多件遗物，直到选择其他选项离开。
        keep_merchant_open = (name == "过路商人" and text.startswith("遗物交易")
                              and bool(res.get("trade")))
        if not keep_merchant_open:
            self.event_pool.resolve(name)
            # 一次性地点事件结算后关闭地点；祭坛与商人按既有规则可重复遭遇。
            if name not in {"祭坛", "过路商人"} and getattr(self.state, "world_state", {}):
                try:
                    world = self._world_from_state()
                    for location_id, location in world.map.locations.items():
                        if location.get("event_name") == name:
                            location["active"] = False
                            world.deactivate_location_sightings(
                                location_id, reason="event_completed")
                    self.state.world_state = world.to_dict()
                except (ValueError, KeyError, TypeError):
                    pass
        # 扭曲都市完成事件后附赠【发现】：正式随机只走DiceEngine，并等待显式选1。
        # 连续交易期间尚未离开商人，不重复发放事件完成奖励。
        bonus = None
        if (not keep_merchant_open and self.state.current_region == "扭曲都市"
                and self.state.player and self.state.player.is_alive):
            owned = {c.name for c in self.state.consumables}
            pool = [n for n in TWISTED_TOOL_LIBRARY if n not in owned]
            if pool:
                discovery = self._offer_item_discovery(pool, f"事件【{name}】完成后附赠发现")
                if not discovery.get("success"):
                    return discovery
                bonus = {"候选": discovery["choices"], "等待选择": True,
                         "来源": discovery["source"]}
        result_payload = {"option": opt["text"], "applied": res["applied"], "instructions": res["instructions"],
                          "shards": self.state.shards,
                          "player_hp": self.state.player.current_hp if self.state.player else None}
        for key in ("random", "outcome", "net_delta", "debt_result", "trade"):
            if key in res:
                result_payload[key] = res[key]
        if name == "过路商人":
            result_payload["merchant_session_open"] = keep_merchant_open
        if bonus:
            result_payload["附赠发现"] = bonus
        next_event = None
        if self.state.pending_event_queue and not keep_merchant_open:
            next_event = self.state.pending_event_queue.pop(0)
            self.event_pool.current = next_event
            queued = self.event_pool.events[next_event]
            result_payload["next_event"] = {
                "event": next_event,
                "region": queued["region"],
                "desc": queued["desc"],
                "options": [{"id": option["id"], "text": option["text"]}
                            for option in queued["options"]],
                "queued_events_remaining": len(self.state.pending_event_queue),
            }
        if getattr(self.state, "world_state", {}):
            try:
                world = self._world_from_state()
                self._sync_world_entity_positions(world)
                self.state.world_state = world.to_dict()
            except (ValueError, KeyError, TypeError):
                pass
        return {
            "success": True, "action": f"事件【{name}】选项{option_id}",
            "result": result_payload,
            "completed_exploration": next_event is None,
            "instruction": (f"请继续结算探索队列中的事件【{next_event}】"
                            if next_event else "本次探索事件已全部结算"),
        }

    # ==================== 回合管理 ====================

    # 语言命令白名单：指令动作与目标/道纹提取。解析严格，解析不出即拒绝并给合法示例。
    _ALLY_ATTACK_VERBS = ("攻击", "打")
    _ALLY_DAOWEN_VERBS = ("发动", "用", "使用")
    _ALLY_GUARD_VERBS = ("护卫", "保护我", "挡伤", "替我挡")

    @ACTION_REGISTRY.action("read_death_book", group="rulings")
    def _action_read_death_book(self, params: dict) -> dict:
        """翻阅《死者之书》：读回全部〖死者之页〗（生平＋遗言）与已积累的智慧条目。

        纯查询：不消耗精力、不掷骰、不改任何数值——《死者之书》与轮回者灵魂绑定
        （《故事文档》世界观·死者之书与微光者），本就随身携带，翻阅不花代价。
        在此之前死者之页只有「写入」（死者之页 → `_commit_death_ruling`）没有「读取」：
        `state.death_book_legacies` 只被序列化与计数，轮回者/AI 一条也看不到。
        """
        legacies = [dict(entry) for entry in self.state.death_book_legacies]
        return {
            "success": True,
            "action": "翻阅死者之书",
            "legacies": legacies,
            "total_legacies": len(legacies),
            "wisdom": list(self.state.death_book_wisdom),
            "path": str(self.death_book.path),
            "instruction": "已读回《死者之书》各页（生平＋遗言），可作为本轮决策的前车之鉴",
        }

    # ==================== DM裁定接口 ====================
