"""协力施法：一条法术开 N 个空位，场上角色逐格填道纹。

规则口径（原文「出手数=空格数，填 X 个道纹消耗 X 次出手构成法术；
法术生效前其他角色可在空位填道纹」）：

* **发起（declare_spell）**：施法者声明法术的槽位模板——若干道纹空位，可在
  空位之间插「（条件）」分支。发起本身不花出手；一名施法者同时只能挂一条。
* **填入（fill_spell）**：场上**任意存活角色（双方都可以）**往一个空位填入
  自己持有的道纹；这是一次主动出手，由**填入者**支付 1 手。
  填入只登记、不结算——法术在发动前不产生任何效果。
* **发动（release_spell）**：施法者宣布发动，法术按瞬发法术管线逐步生效；
  **空位＝空白，直接跳过**。发动本身不花出手（出手已在填入时逐格付清）；
  其他角色填入的道纹由施法者「借」来施放（黄符同款临时授予，结算后回收），
  道纹自身的资源代价按既有规则由施法者支付，步骤之间照常留敌方行动空档。
* **取消（cancel_spell）**：施法者可随时取消；已填入的出手不退。

条件分支沿用瞬发法术 DSL 的「若<条件>则(…)否则(…)」句式（条件主语只能是
自身/施法者，不能用目标）；界面上的 `(条件)→道纹空格→空` 直接映射到它。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..action_registry import ACTION_REGISTRY
from ..enums import CombatSubphase, GamePhase
from ..spell_dsl import SpellDslError, parse_condition

#: 单条协力法术的道纹空位上限：防浏览器提交天文数字；规则本身不限阶。
MAX_COOP_SLOTS = 24

_TARGET_MODE_CLAUSE = {
    # target_mode -> 流程子句的目标身份写法（与 spell_dsl 的 _TARGET_ALIASES 对齐）
    "self": "于自身",
    "caster": "于施法者",
    "target": "于目标",
    "any": "于任意目标",
}


def _coop_flat_slots(template: List[dict]) -> List[dict]:
    """按深度优先顺序摊平模板里的全部道纹空位（含 if 分支内的）。

    返回列表的下标就是界面/协议里的 ``slot`` 编号；if 节点本身不占号。
    """
    flat: List[dict] = []
    for node in template:
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "daowen":
            flat.append(node)
        elif node.get("kind") == "if":
            flat.extend(_coop_flat_slots(node.get("then") or []))
            flat.extend(_coop_flat_slots(node.get("else") or []))
    return flat


def _coop_validate_template(template: Any) -> Tuple[Optional[str], int]:
    """校验槽位模板；返回 (错误信息, 道纹空位数)。错误信息为 None 表示合法。"""
    if not isinstance(template, list) or not template:
        return ("slots 必须是非空列表（每项 {kind:'daowen'} 或 "
                "{kind:'if',cond:'条件',then:[…],else:[…]}）"), 0
    count = 0

    def walk(nodes: Any, where: str) -> Optional[str]:
        nonlocal count
        if not isinstance(nodes, list):
            return f"{where}必须是槽位列表"
        for node in nodes:
            if not isinstance(node, dict):
                return f"{where}的槽位必须是对象"
            kind = node.get("kind")
            if kind == "daowen":
                count += 1
            elif kind == "if":
                cond = str(node.get("cond") or "").strip()
                if not cond:
                    return f"{where}的条件分支缺少条件文本（cond）"
                try:
                    parse_condition(cond)
                except SpellDslError as exc:
                    return f"{where}的条件【{cond}】无法解析：{exc}"
                for branch in ("then", "else"):
                    error = walk(node.get(branch) or [], f"{where}的分支")
                    if error:
                        return error
            else:
                return f"{where}有未知槽位类型 {kind!r}（只支持 daowen / if）"
        return None

    error = walk(template, "slots")
    if error:
        return error, 0
    if count < 1:
        return "协力法术至少要有 1 个道纹空位", 0
    if count > MAX_COOP_SLOTS:
        return f"协力法术最多 {MAX_COOP_SLOTS} 个道纹空位", 0
    return None, count


class CoopSpellHandlersMixin:
    """协力施法行动处理器。宿主类：`GameEngine`。"""

    # ------------------------------------------------------------------
    # 共用小工具（available_actions 与处理器都要用）
    # ------------------------------------------------------------------

    def _coop_find_open(self, spell_id: str) -> Optional[dict]:
        for spell in self.state.open_spells:
            if spell.get("id") == spell_id and spell.get("status") == "open":
                return spell
        return None

    @staticmethod
    def _coop_spell_summary(spell: dict) -> str:
        total = len(_coop_flat_slots(spell.get("slots") or []))
        filled = len(spell.get("fills") or {})
        name = spell.get("name") or "协力法术"
        return f"{name}（{spell.get('caster_name')}发起，空位{total}，已填{filled}）"

    def _coop_common_gates(self, actor, action_label: str) -> Optional[dict]:
        """协力施法四个动作共用的阶段/存活/轮次门禁。"""
        if self.state.phase != GamePhase.IN_COMBAT.value:
            return {"success": False, "error": "协力施法只能在战斗中进行"}
        if self.state.combat_subphase != CombatSubphase.PLAYER_ACTIONS.value:
            return {"success": False, "error": "协力施法只能在行动阶段操作"}
        if actor is None or not actor.is_alive or actor.has_retreated:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if self.state.is_sealed_away(actor):
            return {"success": False, "error": f"{actor.name}正处于【封印】暂离，不能{action_label}"}
        duel_err = self._check_duel_turn_or_error(actor)
        if duel_err:
            return duel_err
        caozuo_error = self.combat.caozuo_check(actor, action_label)
        if caozuo_error:
            return caozuo_error
        if not self.combat.can_act(actor, action_type="fill_spell"):
            error = (f"{actor.name}受【束缚】影响，只能进行法术行动"
                     if actor.has_status("束缚") else f"{actor.name}当前无法{action_label}")
            return {"success": False, "error": error}
        return None

    # ------------------------------------------------------------------
    # 发起
    # ------------------------------------------------------------------

    @ACTION_REGISTRY.action("declare_spell", group="daowen")
    def _action_declare_spell(self, params: dict) -> dict:
        """发起协力法术：开 N 个道纹空位（可含条件分支），不花出手、不结算。"""
        refs = self.combat._combat_entity_refs()
        actor = refs.get(str(params.get("actor_ref") or "player:0"))
        gate = self._coop_common_gates(actor, "发起协力施法")
        if gate:
            return gate

        raw_slots = params.get("slots")
        if isinstance(raw_slots, bool):
            return {"success": False, "error": "slots 必须是正整数或槽位模板列表"}
        if isinstance(raw_slots, int):
            if raw_slots < 1:
                return {"success": False, "error": "空位数必须是≥1整数"}
            if raw_slots > MAX_COOP_SLOTS:
                return {"success": False, "error": f"协力法术最多 {MAX_COOP_SLOTS} 个道纹空位"}
            template: List[dict] = [{"kind": "daowen"} for _ in range(raw_slots)]
        else:
            error, _count = _coop_validate_template(raw_slots)
            if error:
                return {"success": False, "error": error}
            template = [dict(node) for node in raw_slots]

        existing = next((spell for spell in self.state.open_spells
                         if spell.get("status") == "open"
                         and spell.get("caster_ref") == str(params.get("actor_ref") or "player:0")),
                        None)
        if existing:
            return {"success": False,
                    "error": f"{actor.name}已有一条未发动的协力法术"
                             f"（{self._coop_spell_summary(existing)}），先发动或取消它"}

        cast_target_ref = str(params.get("cast_target_ref") or "")
        cast_target_name = ""
        if cast_target_ref:
            target = refs.get(cast_target_ref)
            if target is None or not target.is_alive:
                return {"success": False, "error": "cast_target_ref不是当前存活实体"}
            cast_target_name = target.name

        spell_id = f"coop-{len(self.state.open_spells) + 1}"
        slots = _coop_flat_slots(template)
        spell = {
            "id": spell_id,
            "name": str(params.get("name") or "").strip()[:20] or "协力法术",
            "caster_ref": str(params.get("actor_ref") or "player:0"),
            "caster_name": actor.name,
            "slots": template,
            "slot_count": len(slots),
            "fills": {},
            "cast_target_ref": cast_target_ref,
            "cast_target_name": cast_target_name,
            "round": int(self.state.current_round),
            "status": "open",
        }
        self.state.open_spells.append(spell)
        return {
            "success": True,
            "action": f"{actor.name}发起协力法术【{spell['name']}】",
            "result": {"spell_id": spell_id, "slot_count": len(slots),
                       "cast_target": cast_target_name or None,
                       "note": "发起不花出手；场上任意存活角色（双方都可以）可往空位填入道纹，"
                               "每格由填入者花 1 次出手；发动（release_spell）时空位＝空白跳过"},
        }

    # ------------------------------------------------------------------
    # 填入
    # ------------------------------------------------------------------

    @ACTION_REGISTRY.action("fill_spell", group="daowen")
    def _action_fill_spell(self, params: dict) -> dict:
        """往协力法术的空位填入一枚道纹：登记决策，填入者支付 1 次出手。"""
        refs = self.combat._combat_entity_refs()
        actor = refs.get(str(params.get("actor_ref") or "player:0"))
        gate = self._coop_common_gates(actor, "填入协力法术")
        if gate:
            return gate

        spell = self._coop_find_open(str(params.get("spell_id") or ""))
        if spell is None:
            return {"success": False, "error": "spell_id不是当前未发动的协力法术"}

        flat = _coop_flat_slots(spell["slots"])
        slot = params.get("slot")
        if not isinstance(slot, int) or isinstance(slot, bool) or not (0 <= slot < len(flat)):
            return {"success": False,
                    "error": f"slot 必须是 0~{len(flat) - 1} 的空位编号（按界面深度优先编号）"}
        key = str(slot)
        if key in spell["fills"]:
            filler = spell["fills"][key].get("filled_by_name", "别人")
            return {"success": False, "error": f"空位{slot}已被{filler}填入"}

        name = str(params.get("daowen_name") or "")
        if not name:
            return {"success": False, "error": "必须提交 daowen_name"}
        if name not in actor.dao_wen:
            return {"success": False, "error": f"{actor.name}未持有道纹: {name}"}
        if not actor.can_use_daowen(name):
            return {"success": False, "error": f"道纹{name}不可用（冷却/封印/【唯一】锁死）"}

        x = params.get("x", 1)
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "x 必须是≥1整数（本格道纹的X值）"}

        mode = str(params.get("target_mode") or "target")
        if mode not in _TARGET_MODE_CLAUSE:
            return {"success": False,
                    "error": f"target_mode 必须是 {'/'.join(_TARGET_MODE_CLAUSE)}"}
        target_ref = ""
        target_name = ""
        if mode == "any":
            target_ref = str(params.get("target_ref") or "")
            target = refs.get(target_ref)
            if target is None or not target.is_alive:
                return {"success": False, "error": "于任意目标的格子必须提交存活的 target_ref"}
            target_name = target.name
        elif mode == "target":
            if not spell.get("cast_target_ref"):
                return {"success": False,
                        "error": "这条法术发起时没有指定[目标]；该格请改用 self/caster/any"}
            cast_target = refs.get(spell["cast_target_ref"])
            if cast_target is None or not cast_target.is_alive:
                return {"success": False, "error": "法术的[目标]已不在场上，不能填于目标格"}

        dodge = params.get("dodge", False)
        if not isinstance(dodge, bool):
            return {"success": False, "error": "dodge 必须是布尔值"}
        extra = params.get("extra") or {}
        if not isinstance(extra, dict):
            return {"success": False, "error": "extra 必须是对象（该步的附加决策，结构同瞬发法术 steps）"}

        # 填入就是一次主动出手：由填入者支付（谁填谁付）。
        error = self._consume_actions_or_error(actor, 1, action_label="填入协力法术空位",
                                               action_type="fill_spell")
        if error:
            return error

        spell["fills"][key] = {
            "daowen_name": name, "x": int(x), "target_mode": mode,
            "target_ref": target_ref, "target_name": target_name,
            "dodge": dodge, "extra": dict(extra),
            "filled_by": str(params.get("actor_ref") or "player:0"),
            "filled_by_name": actor.name,
        }
        remaining = len(flat) - len(spell["fills"])
        return {
            "success": True,
            "action": f"{actor.name}把【{name}X={x}】填入协力法术【{spell['name']}】空位{slot}",
            "result": {"spell_id": spell["id"], "slot": slot, "daowen": name, "x": int(x),
                       "filled": len(spell["fills"]), "remaining": remaining,
                       "note": "发动前不结算任何效果；施法者用 release_spell 发动，空白格自动跳过"},
        }

    # ------------------------------------------------------------------
    # 发动
    # ------------------------------------------------------------------

    def _coop_build_release(self, spell: dict, refs: Dict[str, Any]) -> Tuple[Optional[str],
                                                                             Optional[list],
                                                                             Optional[str]]:
        """把已填格编译成（flow 文本, steps 决策, 错误）。

        空位＝空白直接跳过；条件分支两臂都没填 → 整个分支跳过；
        只有「否则」臂有内容 → 取反条件改写成「若非(条件)则(…)」。
        步骤顺序与 spell_dsl.iter_action_slots 的深度优先顺序严格一致。
        """
        clauses: List[str] = []
        steps: List[dict] = []
        counter = [0]

        def emit_daowen(fill: dict) -> None:
            mode = fill.get("target_mode") or "target"
            clause = f"发动{fill['daowen_name']}X{_TARGET_MODE_CLAUSE[mode]}"
            step: dict = {"x": int(fill["x"]), "dodge": bool(fill.get("dodge"))}
            if mode == "any":
                step["target_ref"] = fill.get("target_ref")
            if fill.get("extra"):
                step.update(fill["extra"])
            clauses.append(clause)
            steps.append(step)

        def walk(nodes: List[dict]) -> None:
            for node in nodes:
                if node.get("kind") == "daowen":
                    index = counter[0]
                    counter[0] += 1
                    fill = spell["fills"].get(str(index))
                    if fill:
                        emit_daowen(fill)
                    continue
                # if 分支：then/else 各自递归；两臂全空 → 整支跳过
                then_nodes = node.get("then") or []
                else_nodes = node.get("else") or []
                then_mark, else_mark = len(clauses), len(clauses)
                walk(then_nodes)
                then_end = len(clauses)
                walk(else_nodes)
                then_clauses = clauses[then_mark:then_end]
                else_clauses = clauses[then_end:]
                cond = str(node.get("cond") or "").strip()
                if not then_clauses and not else_clauses:
                    # 两臂都空白：整支跳过，但编号计数器仍要越过这些格子
                    continue
                del clauses[then_mark:]
                if then_clauses:
                    clause = f"若{cond}则（{'；'.join(then_clauses)}）"
                    if else_clauses:
                        clause += f"否则（{'；'.join(else_clauses)}）"
                    clauses.append(clause)
                else:
                    # 只有「否则」臂有内容：取反条件，保持步骤顺序不变
                    clauses.append(f"若非({cond})则（{'；'.join(else_clauses)}）")

        walk(spell["slots"])
        if not clauses:
            return None, None, "所有空位都是空白，没有可发动的道纹；要放弃请用 cancel_spell"
        return "→".join(clauses), steps, None

    @ACTION_REGISTRY.action("release_spell", group="daowen")
    def _action_release_spell(self, params: dict) -> dict:
        """发动协力法术：按瞬发法术管线执行已填格，空白格跳过；发动不花出手。"""
        spell_id = str(params.get("spell_id") or "")
        spell = self._coop_find_open(spell_id)
        if spell is None:
            return {"success": False, "error": "spell_id不是当前未发动的协力法术"}
        refs = self.combat._combat_entity_refs()
        actor_ref = str(params.get("actor_ref") or spell["caster_ref"])
        if actor_ref != spell["caster_ref"]:
            return {"success": False,
                    "error": f"只有施法者（{spell['caster_name']}）能发动这条协力法术"}
        actor = refs.get(actor_ref)
        gate = self._coop_common_gates(actor, "发动协力法术")
        if gate:
            return gate

        flow_text, steps, error = self._coop_build_release(spell, refs)
        if error:
            return {"success": False, "error": error}

        # 其他角色填入的道纹由施法者「借」来施放：黄符同款临时授予，
        # 结算完成/中断后由 cast_instant 统一回收（契约错误则就地回收）。
        from ..models import DaoWen, DaoWenInstance
        grant_daowen = sorted({fill["daowen_name"] for fill in spell["fills"].values()}
                              - set(actor.dao_wen))
        for name in grant_daowen:
            actor.dao_wen[name] = DaoWenInstance(DaoWen(
                name=name, formula="", cost_type="", cost_formula="", effect_formula=""))
        result = self._action_cast_instant({
            "actor_ref": actor_ref,
            "flow": flow_text,
            "target_ref": spell.get("cast_target_ref") or None,
            "steps": steps,
            "name": spell["name"],
            "coop_release": {"spell_id": spell_id, "temp_granted": grant_daowen},
        })
        if not result.get("success"):
            # 契约错误/结算失败都不会压栈（压栈路径必返回 success），就地回收。
            for name in grant_daowen:
                actor.dao_wen.pop(name, None)
            return result
        spell["status"] = "released"
        self.state.open_spells = [s for s in self.state.open_spells if s is not spell]
        payload = result.setdefault("result", {})
        payload["coop"] = {
            "spell_id": spell_id, "flow": flow_text,
            "fillers": sorted({fill["filled_by_name"] for fill in spell["fills"].values()}),
            "temp_granted": grant_daowen,
            "note": "协力法术：出手已在填入时由各填入者支付，发动不再扣出手",
        }
        return result

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    @ACTION_REGISTRY.action("cancel_spell", group="daowen")
    def _action_cancel_spell(self, params: dict) -> dict:
        """取消协力法术：从场上移除；已填入的出手不退（填入时已作为行动结算）。"""
        spell_id = str(params.get("spell_id") or "")
        spell = self._coop_find_open(spell_id)
        if spell is None:
            return {"success": False, "error": "spell_id不是当前未发动的协力法术"}
        actor_ref = str(params.get("actor_ref") or spell["caster_ref"])
        if actor_ref != spell["caster_ref"]:
            return {"success": False,
                    "error": f"只有施法者（{spell['caster_name']}）能取消这条协力法术"}
        spell["status"] = "cancelled"
        self.state.open_spells = [s for s in self.state.open_spells if s is not spell]
        return {
            "success": True,
            "action": f"{spell['caster_name']}取消协力法术【{spell['name']}】",
            "result": {"spell_id": spell_id,
                       "note": "已填入的出手不退：填入当时就是一次完整行动"},
        }
