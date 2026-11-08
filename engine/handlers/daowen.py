"""道纹与法术类行动：发动道纹、施法、定义/废弃法术、残韵、点档道纹、炼心。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations

from .. import custom_resources
from ..action_registry import ACTION_REGISTRY
from ..consumables import note_action_targets
from ..daowen import DaoWenEngine, ResonanceEngine
from ..enums import CombatSubphase, GamePhase
from ..gamedata import learnability_error
from ..models import DaoWen, DaoWenInstance


class DaowenHandlersMixin:
    """道纹/法术行动处理器。宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("use_daowen", group="daowen")
    def _action_use_daowen(self, params: dict) -> dict:
        """
        发动道纹。
        params.actor 留空时=玩家自行发动道纹(法力制，行为与此前完全一致)。
        params.actor 指定为已部署[朋友]/[员工]时=听从轮回者指令代其发动：
        1.[朋友]/[员工]**照常支付法力**（2026-09-17 用户令裁定；旧版"不持有法力、
          不支付法力"是已废止条文的残留，见 AI_EXPERIENCE.md:278/:1254 微光者持有
          [法限]且为一池制），并额外消耗其出手；
          附带【代价】的道纹仍照常由该实体自身支付代价。
        2.必须指定一个不是其自身的目标(听从指令的道纹/攻击均需面向"其他非自身目标")。
        """
        actor_ref = params.get("actor_ref", "")
        actor_name = params.get("actor", "")  # 旧存档/外部调用仅在名称唯一时兼容
        is_command = False
        refs = self.combat._combat_entity_refs()
        if actor_ref:
            actor = refs.get(actor_ref)
        elif actor_name:
            matches = [entity for entity in refs.values() if entity.name == actor_name]
            actor = matches[0] if len(matches) == 1 else None
        else:
            actor = self.state.player
        # 2026-10-08：被【封印】暂离的实体不在场上，不能发动道纹（含封印自己后
        # 靠默认 actor=player 绕开 refs 的路径——上面 `actor = self.state.player`
        # 是直取单字段，不过 _combat_entity_refs，必须在这里单独挡一道）。
        if self.state.is_sealed_away(actor):
            return {"success": False, "error": f"{actor.name}正处于【封印】暂离，不能发动道纹"}
        if actor is None or not actor.is_alive or actor.has_retreated:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if actor is self.state.player:
            pass
        elif (self.state.in_final_duel and actor.entity_type == "轮回者"
              and actor in self.state.get_all_enemy_side()):
            pass
        elif actor in self.state.friends or actor in [e for e in self.state.employees if e.is_deployed]:
            is_command = True
        elif actor in self.state.get_all_enemy_side():
            pass  # 交替出手中，双方角色共用同一主动发动入口
        else:
            return {"success": False, "error": f"{actor.name}不能作为use_daowen的行动者"}

        name = params.get("daowen_name", "")
        x = params.get("x", 1)
        # 【操纵】：发动道纹不是「施法」，属于被【操纵】管辖的非施法行动。
        caozuo_error = self.combat.caozuo_check(actor, "发动道纹")
        if caozuo_error:
            return caozuo_error
        if name == "唤潮":
            return self._action_tidecall(params, actor)
        if name == "蜃景":
            return self._action_shenjing(params, actor)
        target_name = params.get("target", "")
        target_ref = params.get("target_ref", "")

        if name not in actor.dao_wen:
            return {"success": False, "error": f"{actor.name}未持有道纹: {name}"}

        # X=0 = 拒绝发动。
        # 轮到发动却不想发动的道纹，用 X=0 显式放弃：视为无效，不支付代价、
        # 不消耗出手、不进入任何结算，也不报错。与「轮空/让行」同一语义：
        # 是一次合法的"我放弃这次机会"，而不是一次失败的尝试。
        # 位置在 can_use() 之前：正在冷却/被封印的道纹同样有权被拒绝。
        if x == 0 and not isinstance(x, bool):
            return {
                "success": True, "action": "use_daowen", "skipped": True,
                "result": {"daowen": name, "x": 0, "skipped": True, "actor": actor.name,
                           "note": f"X=0：【{name}】视为无效，{actor.name}放弃本次发动"
                                   f"（不消耗出手、不支付代价）"},
            }

        dw_instance = actor.dao_wen[name]

        if name == "真理":
            # 初拥能力迁成道纹身份，但其既有主动入口不消耗出手、不附加数值效果。
            return self._action_use_truth_daowen(params, actor=actor, x=x)

        if not dw_instance.can_use():
            return {"success": False, "error": f"道纹{name}不可用（冷却/封印）"}
        # 【唯一】（2026-11-02 修复1）：规则阻断走明确文案，不再靠计算层抛异常。
        if actor.daowen_locked_by_unique(name):
            return {"success": False,
                    "error": f"【唯一】生效中：{actor.name}已经用过【{name}】，不能再次使用"}

        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "X必须≥1且为整数"}
        if is_command and dw_instance.x_value > 0 and x != dw_instance.x_value:
            return {"success": False, "error": f"{actor.name}的【{name}】固定为X={dw_instance.x_value}"}

        # 计算函数声明了target即正文含[目标]：缺少显式目标时判定失效，禁止静默改为自身。
        import inspect
        DaoWenEngine.register_all()
        requires_target = "target" in inspect.signature(DaoWenEngine._registry[name]).parameters
        if name in ("波及", "共罪") and not (target_ref or target_name):
            # 多目标道纹：从dodge_targets取首个引用作为通用目标。
            submitted = list(params.get("dodge_targets") or [])
            if submitted and isinstance(submitted[0], dict) and submitted[0].get("target_ref"):
                target_ref = submitted[0]["target_ref"]
        if requires_target and not (target_ref or target_name):
            return {"success": False, "error": f"【{name}】需要显式指定目标target_ref；缺少[目标]时失效"}

        if is_command and not (target_ref or target_name):
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身的目标"}

        # 使用稳定引用定位目标；旧名称仅在唯一匹配时兼容。
        refs = self.combat._combat_entity_refs()
        target = actor
        if target_ref:
            target = refs.get(target_ref)
            if target is None:
                return {"success": False, "error": "target_ref不是当前合法实体"}
        elif target_name:
            matches = [entity for entity in refs.values() if entity.name == target_name]
            if len(matches) != 1:
                return {"success": False, "error": f"目标名称{target_name}不唯一或不存在，请改用target_ref"}
            target = matches[0]
        try:
            target, tamper_rule = self.combat.resolve_tampered_target(actor, target, name, params, refs)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if tamper_rule is not None:
            params = dict(params, target_ref=next(ref for ref, entity in refs.items() if entity is target), target="")
            target_ref = params["target_ref"]
        if is_command and target is actor:
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身目标"}
        if name == "共罪":
            # 2026-10-31 用户裁定：共罪选 X 个非自身目标（不再固定 2 个）。
            pact_entries = list(params.get("dodge_targets") or [])
            if len(pact_entries) != x:
                return {"success": False, "error": f"共罪X={x}必须显式选择恰好{x}个目标"}
            pact_refs = [entry.get("target_ref") for entry in pact_entries
                         if isinstance(entry, dict)]
            if len(pact_refs) != x or len(set(pact_refs)) != x:
                return {"success": False, "error": f"共罪目标必须是{x}个互不重复的target_ref"}
            if any(ref == str(actor.runtime_id) for ref in pact_refs):
                return {"success": False, "error": "共罪目标必须是非自身目标"}
            if any(ref not in refs or not refs[ref].is_alive
                   or not self.combat.is_targetable(actor, refs[ref])
                   for ref in pact_refs):
                return {"success": False, "error": "共罪目标必须为当前可选的存活角色"}
            if any(("dodge" in entry and not isinstance(entry["dodge"], bool))
                   or ("blood_shadow" in entry and not isinstance(entry["blood_shadow"], bool))
                   or entry.get("dodge", False) and entry.get("blood_shadow", False)
                   for entry in pact_entries):
                return {"success": False, "error": "共罪目标的dodge/blood_shadow必须为互斥布尔值"}
        if name == "操纵":
            if not (target_ref or target_name):
                return {"success": False, "error": "操纵需要显式指定的[目标]"}
            try:
                from ..daowen import normalize_caozuo_order
                params = dict(params, caozuo_order=normalize_caozuo_order(
                    params.get("caozuo_order")))
            except ValueError as exc:
                return {"success": False, "error": f"操纵指令无效：{exc}"}
        if name in ("赦免", "贿赂"):
            picked = [str(n) for n in (params.get("relic_names") or []) if n]
            if len(picked) != x or len(set(picked)) != x:
                return {"success": False,
                        "error": f"{name}X={x}必须显式指定{x}件互不重复的遗物"}
            if name == "赦免":
                pool = self.state.relics if actor is self.state.player else getattr(actor, "relics", [])
                known = {r.name for r in pool}
                missing = [n for n in picked if n not in known]
                if missing:
                    return {"success": False, "error": f"赦免只能启用自己持有的遗物：未持有{missing}"}
            else:
                tgt = refs.get(target_ref) if target_ref else None
                if tgt is None:
                    return {"success": False, "error": "贿赂需要显式指定target_ref"}
                pool = self.state.relics if tgt is self.state.player else getattr(tgt, "relics", [])
                enabled = {r.name for r in pool if getattr(r, "enabled", True)}
                missing = [n for n in picked if n not in enabled]
                if missing:
                    return {"success": False, "error": f"贿赂只能作用于目标已启用的遗物：不符{missing}"}

        if target is not actor and not self.combat.is_targetable(actor, target):
            locked_by = self.combat.yapo_target_lock_holder(actor, target)
            if locked_by is not None:
                return {"success": False,
                        "error": f"【压迫】生效中：{actor.name}只能以自身为[目标]（豁免目标：{locked_by.name}）"}
            return {"success": False, "error": f"{target.name}处于飞行，无法被选中"}
        # 自愈X「目标死亡时」：显式目标（默认自身不算选中；波及X 记全部已提交目标）。
        _dw_selected = [target] if (target_ref or target_name) else []
        for _dt in params.get("dodge_targets") or []:
            if isinstance(_dt, dict) and _dt.get("target_ref"):
                _ent = refs.get(_dt["target_ref"])
                if _ent is not None:
                    _dw_selected.append(_ent)

        duel_error = self._check_duel_turn_or_error(actor)
        if duel_error:
            return duel_error
        if self.state.phase == "in_combat":
            if not self.combat.can_act(actor):
                return {"success": False, "error": f"{actor.name}当前无法出手"}
            budget = self._action_budget_of(actor)
            if actor.actions_used_this_round >= budget:
                return {"success": False,
                        "error": f"{actor.name}本回合出手已用完({actor.actions_used_this_round}/{budget})"}

        # 调用道纹引擎计算
        # 保留通用第二参数通道；现行【分裂】只有X（造物数量），不再读取Y。
        daowen_y = params.get("y")
        if not isinstance(daowen_y, int) or isinstance(daowen_y, bool) or daowen_y < 1:
            daowen_y = 1
        freeze_choices = None
        if name == "冰冻":
            try:
                effective_x, freeze_choices = self.combat.prepare_bingdong(
                    target, x, params.get("freeze_daowen"))
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            if effective_x == 0:
                return {"success": True, "action": "use_daowen", "skipped": True,
                        "result": {"daowen": name, "x": 0, "skipped": True,
                                   "note": "目标没有道纹，冰冻X自动降为0；不支付冷却、不消耗出手"}}
            x = effective_x
            params = dict(params, x=x)
        try:
            resolve_kw = {"target": target, "caster": actor}
            if name == "净化":
                resolve_kw["cost_choices"] = params.get("cost_choices")
            resolve_kw["y"] = daowen_y
            # 2026-10-14：【透支】多支付通道（缺省生命；resolve 按签名分发，其余道纹无感）。
            if name == "透支":
                resolve_kw["cost_channel"] = params.get("touzhi_cost", "生命")
            # 2026-10-22：【连击】多支付通道（缺省生命；与透支同一接线口径）。
            if name == "连击":
                resolve_kw["cost_channel"] = params.get("lianji_cost", "生命")
            # 2026-10-31：【怜悯】封锁一种支付途径（缺省法力）。
            if name == "怜悯":
                resolve_kw["blocked_type"] = params.get("lianmin_cost", "法力")
            if name in ("赦免", "贿赂"):
                resolve_kw["relic_names"] = list(params.get("relic_names") or [])
            if name == "操纵":
                # 剩余次数＝X（2026-11-02 用户令：由「下一次」改为「下X次」）。
                resolve_kw["order"] = params.get("caozuo_order")
            if name == "再生":
                resolve_kw["cost_channel"] = params.get("zaisheng_cost", "法力")
            # 2026-11-08 四副本补齐：带显式选择的道纹把选择透传给计算层。
            if name in ("重铸", "回炉"):
                resolve_kw["item_name"] = str(params.get("item_name") or "")
            if name == "借尸":
                resolve_kw["corpse_name"] = str(params.get("corpse_name") or "")
            if name == "索取":
                resolve_kw["resource_type"] = str(params.get("resource_type") or "法力")
            if name == "感染":
                resolve_kw["daowen_names"] = list(params.get("daowen_names") or [])
            if name == "重演":
                resolve_kw["replay_name"] = str(
                    params.get("replay_daowen") or params.get("replay_name") or "")
            if name == "病变":
                resolve_kw["from_kind"] = str(params.get("from_kind") or "")
                resolve_kw["to_kind"] = str(params.get("to_kind") or "")
            if name == "勾魂":
                resolve_kw["daowen_names"] = list(params.get("daowen_names") or [])
            calc = DaoWenEngine.resolve(name, x, **resolve_kw)
            if name == "冰冻":
                calc["freeze_daowen"] = freeze_choices
        except Exception as e:
            return {"success": False, "error": f"道纹计算失败: {str(e)}"}
        if calc.get("cost_type") == "攻击力" and calc.get("cost_attack", 0) > actor.effective_attack_power():
            return {"success": False,
                    "error": f"再生攻击力代价需要{calc.get('cost_attack', 0)}点，{actor.name}当前有效攻击力为{actor.effective_attack_power()}"}
        if calc.get("cost_type") == "失忆" and calc.get("cost", 0) > len(actor.dao_wen):
            return {"success": False,
                    "error": f"失忆{calc.get('cost', 0)}无法支付：{actor.name}仅持有{len(actor.dao_wen)}种道纹"}
        if int(calc.get("forget_daowen") or 0) > 0:
            # 2026-11-01 用户令：失忆必须显式选纹，严禁按持有顺序替玩家代选。
            # 校验放在任何支付之前——不合法的选择不扣法力、不扣碎片、不消耗出手。
            try:
                calc["forget_daowen_names"] = self.combat.resolve_forget_daowen(
                    actor, name, int(calc["forget_daowen"]), params.get("forget_daowen"))
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

        if name == "招魂":
            # 2026-11-01 用户令（修复6）：场上没有可唤回的怪物尸体时，必须在
            # **扣法力前**拒绝，不能先付3X法力再记一句"没有可唤回的怪物尸体"。
            # 2026-11-01 重构3.1：多具尸体时**允许**调用方用 revive_monster 指定
            # 唤回哪一具；未指定（或只有一具）时默认取最近一具（dead_monsters[-1]）。
            corpses = [e for e in self.state.dead_monsters
                       if getattr(e, "entity_type", "") == "怪物"]
            if not corpses:
                return {"success": False,
                        "error": "【招魂】场上没有可唤回的怪物尸体，本次发动不成立，不支付法力"}
            corpse_names = [e.name for e in corpses]
            wanted = str(params.get("revive_monster") or "").strip()
            if wanted and wanted not in corpse_names:
                return {"success": False,
                        "error": f"【招魂】没有名为「{wanted}」的怪物尸体，可选 {corpse_names}"}
            calc["revive_monster_name"] = wanted or corpse_names[-1]

        if name == "豪夺":
            # 2026-11-01 用户令：无可夺遗物必须在**扣费前**中止，禁止吞掉5X碎片空放；
            # 多件可夺时玩家必须显式提交 relic_name（唯一候选才允许省略）。
            if target is actor:
                return {"success": False,
                        "error": "【豪夺】必须以他人为[目标]：夺取自身的遗物不产生任何转移"}
            pool = self.combat._item_steal_candidates(target)
            names = [n for _k, n in pool]
            if not pool:
                return {"success": False,
                        "error": f"【豪夺】{target.name}没有可夺取的遗物或消耗品"
                                 f"（已封印/已被夺走的不算），本次发动不成立，不支付任何代价"}
            wanted = str(params.get("relic_name") or params.get("haoduo_relic")
                         or params.get("item_name") or "").strip()
            wanted_kind = str(params.get("item_kind") or "").strip()
            if wanted and wanted not in names:
                return {"success": False,
                        "error": f"【豪夺】目标没有可夺取的遗物或消耗品「{wanted}」，可选 {names}"}
            if not wanted:
                if len(pool) > 1:
                    return {"success": False,
                            "error": f"【豪夺】{target.name}有多件可夺遗物或消耗品{names}，"
                                     f"必须用relic_name显式指定要夺哪一件"}
                wanted_kind, wanted = pool[0]
            else:
                kinds = [k for k, n in pool if n == wanted]
                if wanted_kind and wanted_kind in kinds:
                    pass
                elif len(kinds) > 1:
                    return {"success": False,
                            "error": f"【豪夺】目标同时有遗物与消耗品「{wanted}」，"
                                     f"必须用item_kind指定种类（relic/consumable）"}
                else:
                    wanted_kind = kinds[0]
            calc["haoduo_relic_name"] = wanted
            calc["haoduo_item_kind"] = wanted_kind

        if name in ("重铸", "回炉"):
            # 与【豪夺】同口径：无可操作消耗品/多件未指定时必须在扣费前拒绝。
            item_names = [i.name for i in self.combat._consumable_holder_list(target)
                          if getattr(i, "current_uses", None) is not None]
            if not item_names:
                return {"success": False,
                        "error": f"【{name}】{target.name}没有消耗品，本次发动不成立，不支付代价"}
            wanted_item = str(params.get("item_name") or "").strip()
            if wanted_item and wanted_item not in item_names:
                return {"success": False,
                        "error": f"【{name}】目标没有消耗品「{wanted_item}」，可选 {item_names}"}
            if not wanted_item and len(item_names) > 1 and actor is self.state.player:
                return {"success": False,
                        "error": f"【{name}】{target.name}有多件消耗品{item_names}，必须用item_name显式指定"}
            key = "recast_item_name" if name == "重铸" else "reforge_destroy_item_name"
            calc[key] = wanted_item or item_names[0]

        if name == "借尸":
            corpses = [e for e in getattr(self.state, "dead_monsters", []) or []]
            if not corpses:
                return {"success": False,
                        "error": "【借尸】场上没有尸体，本次发动不成立，不支付代价"}
            corpse_names = [e.name for e in corpses]
            wanted_corpse = str(params.get("corpse_name") or "").strip()
            if wanted_corpse and wanted_corpse not in corpse_names:
                return {"success": False,
                        "error": f"【借尸】场上没有名为「{wanted_corpse}」的尸体，可选 {corpse_names}"}
            calc["borrow_corpse_name"] = wanted_corpse or corpse_names[-1]

        if name == "合体":
            side = (self.state.get_all_player_side()
                    if not self.state.on_enemy_side(actor)
                    else self.state.get_all_enemy_side())
            creations = [e for e in side if e is not actor and e.treated_as_creation()]
            if len(creations) < x:
                return {"success": False,
                        "error": f"【合体】己方只有{len(creations)}个造物，不足{x}个，本次发动不成立，不支付代价"}

        if name == "感染":
            candidates = [n for n in target.dao_wen if n != "感染"]
            if not candidates:
                return {"success": False,
                        "error": f"【感染】{target.name}没有可变为感染的道纹，本次发动不成立，不支付代价"}
            picked = [str(n) for n in (params.get("daowen_names") or []) if n]
            missing = [n for n in picked if n not in candidates]
            if missing:
                return {"success": False,
                        "error": f"【感染】目标未持有可感染的道纹{missing}，持有 {candidates}"}
            if not picked and len(candidates) > x and actor is self.state.player:
                return {"success": False,
                        "error": f"【感染】{target.name}持有{len(candidates)}种道纹{candidates}，必须用daowen_names显式指定"}
            calc["infect_daowen_names"] = (picked or candidates[:x])[:x]

        if name == "勾魂":
            # 2026-11-08 重做：转移[目标]的X种道纹至1个[造物]；无造物/未指定
            # 道纹时在扣费前拒绝（与【豪夺】同口径）。
            calc["gouhun_creation_ref"] = str(params.get("creation_ref") or "")
            candidates = list(target.dao_wen)
            if not candidates:
                return {"success": False,
                        "error": f"【勾魂】{target.name}没有可转移的道纹，本次发动不成立，不支付代价"}
            picked = [str(n) for n in (params.get("daowen_names") or []) if n]
            missing = [n for n in picked if n not in candidates]
            if missing:
                return {"success": False,
                        "error": f"【勾魂】目标未持有道纹{missing}，持有 {candidates}"}
            if not picked and len(candidates) > x and actor is self.state.player:
                return {"success": False,
                        "error": f"【勾魂】{target.name}持有{len(candidates)}种道纹{candidates}，必须用daowen_names显式指定"}
            everyone = self.state.get_all_player_side() + self.state.get_all_enemy_side()
            creation_ref = calc["gouhun_creation_ref"]
            creation = refs.get(creation_ref) if creation_ref else None
            if creation_ref and (creation is None or not creation.treated_as_creation()):
                return {"success": False, "error": "【勾魂】creation_ref不是场上的[造物]"}
            if not any(e is not target and e.treated_as_creation() for e in everyone):
                return {"success": False,
                        "error": "【勾魂】场上没有可接收道纹的[造物]，本次发动不成立，不支付代价"}
            calc["transfer_daowen_names"] = (picked or candidates[:x])[:x]

        if name == "病变":
            from ..daowen import purifiable_cost_layers
            src = str(params.get("from_kind") or "")
            dst = str(params.get("to_kind") or "")
            if not src or not dst:
                return {"success": False, "error": "病变必须显式指定from_kind与to_kind"}
            if purifiable_cost_layers(target, src) <= 0:
                return {"success": False,
                        "error": f"【病变】{target.name}未持有可转化的【{src}】层数，本次发动不成立，不支付代价"}

        if name == "重演":
            from ..daowen import DaoWenEngine as _DWE
            history = sorted(n for n in getattr(target, "daowen_use_history", set())
                             if n in _DWE.list_all() and n != "重演")
            if not history:
                return {"success": False,
                        "error": f"【重演】{target.name}本场尚未发动过任何道纹，本次发动不成立，不支付代价"}
            wanted_replay = str(params.get("replay_daowen") or params.get("replay_name") or "").strip()
            if wanted_replay and wanted_replay not in history:
                return {"success": False,
                        "error": f"【重演】{target.name}本场未发动过【{wanted_replay}】，已发动 {history}"}
            if not wanted_replay and len(history) > 1 and actor is self.state.player:
                return {"success": False,
                        "error": f"【重演】{target.name}本场已发动过{history}，必须用replay_daowen显式指定"}
            calc["replay_daowen_name"] = wanted_replay or history[-1]

        # 「发动道纹」的共用前置环节（与法术每一步同一实现，见
        # CombatEngine._daowen_step_preflight）：缄默面具 → 目标发动道纹前反应 →
        # 施法者是否存活。顺序与 cast(flow=...) 的每一步完全相同——这就是
        # R-1 的裁定：法术的一步就是一次发动道纹，只有反应链层数不同。
        from ..spell_execution import InterruptReason as _IR
        early, trigger_logs = self.combat._daowen_step_preflight(
            daowen=name, x=x, calc=calc, caster=actor, target=target, refs=refs,
            entry_extra={"trigger_spell_choices": params.get("trigger_spell_choices", {}),
                         "status_index": params.get("status_index"),
                         "status_indices": params.get("status_indices", {}),
                         "flow_target_ref": params.get("flow_target_ref"),
                         "catalysis_steps": params.get("catalysis_steps")},
            policy=self.combat.CAST_STEP_POLICY)
        if early is not None:
            if early.reason in (_IR.CASTER_DEAD,_IR.CASTER_UNAVAILABLE):
                note_action_targets(self.state, actor, _dw_selected)
                return {"success": True, "action": (f"{actor.name}发动道纹前被反应法术命零" if early.reason==_IR.CASTER_DEAD
                                                     else f"{actor.name}发动道纹前被反应法术移出战场"),
                        "result": {"trigger_spell_logs": trigger_logs, "daowen_resolved": False}}
            return {"success": False, "error": early.detail}

        # 检查法力是否足够（代价道纹不消耗法力）
        # 2026-09-17 用户令裁定：微光者（[朋友]/[员工]）**同样支付法力**。
        # 旧注释称其"不持有法力、发动道纹不支付法力，只消耗出手"，自我论证是
        # 「与怪物规则一致」——而怪物那条已于 2026-09-16 用户令废止
        # （见 engine/combat.py 同位置）。正文 AI_EXPERIENCE.md:278 明载
        # 「怪物与轮回者、微光者共用同一套面板数据，同样持有[血限]/[法限]/[速限]」，
        # :1254 明载微光者「仍是一池制，[回始]不回填」——不支出就无所谓"一池制"。
        # 故三类角色同口径支付【消耗】类法力；微光者仍照常额外消耗其出手。
        #
        # 2026-10-08 结构审查：此处的法力支付原为一份内联副本，口径与法术单步
        # （CombatEngine.pay_daowen_mana）表面相同、实则多一条 `cost_mutation`
        # 兜底，属「同一条规则两处实现」。现统一调用共享实现。
        # 等价性已验证：遍历全部 63 个道纹 × X∈{1,3}，凡 cost_type=="消耗" 的
        # calc 都带 `cost` 键；缺 `cost` 只有 `cost_mutation` 的 6 个道纹
        # （全力/封印/必中/自愈）全部是「异变」类型，不进法力分支，
        # 故该兜底是死代码，删除后数值逐字节不变。
        _cost_paid, mana_error = self.combat.pay_daowen_mana(actor, calc, target)
        if mana_error:
            return {"success": False, "error": mana_error}

        # 碎片类主动道纹代价预检与支付
        shard_error = self._pay_daowen_shard_cost(actor, name, calc, x)
        if shard_error:
            return {"success": False, "error": shard_error}

        # 自定义道纹：法力以外的代价（生命/血限/法限/速度/异变/碎片/冷却…）在占出手前支付，
        # 付不起就当次发动失败、不消耗出手（与内建道纹同一口径）。
        if calc.get("custom_effects") is not None:
            custom_cost_error = custom_resources.pay_custom_cost(
                self, actor, dw_instance, calc,
                forget_daowen=params.get("forget_daowen"))
            if custom_cost_error:
                return {"success": False, "error": custom_cost_error}

        if self.state.phase == "in_combat":
            budget_error = self._consume_action_or_error(actor)
            if budget_error:
                return budget_error

        dodge_value = params.get("dodge", False)
        blood_shadow_value = params.get("blood_shadow", False)
        if not isinstance(dodge_value, bool) or not isinstance(blood_shadow_value, bool):
            return {"success": False, "error": "dodge与blood_shadow必须是布尔值"}
        if dodge_value and blood_shadow_value:
            return {"success": False, "error": "不能同时闪避并使用血影"}
        dodge = dodge_value
        dodge_targets = list(params.get("dodge_targets") or [])
        dodge_log, aoe_targets_override = self._resolve_daowen_dodge(
            name, actor, target, dodge, dodge_targets,
            blood_shadow=bool(params.get("blood_shadow", False)),
            blood_shadow_cost_share_target_ref=params.get(
                "blood_shadow_cost_share_target_ref", ""),
            dodge_relic_target_ref=params.get("dodge_relic_target_ref", ""),
            # 2026-10-31：共罪的目标数=X（不借用波及的 mark_targets 字段）。
            wave_count=(x if name == "共罪" else int(calc.get("mark_targets", 0))),
            activation_x=x,
        )
        self.combat.pay_daowen_cooldown(name,calc,actor)
        if name == "共罪":
            # 共罪：把本次实际提交并接受的目标整组交给效果层，不再写死两个字段。
            calc["targets"] = list(aoe_targets_override or [])
        if dodge_log.get("fully_dodged"):
            missed_numeric_cost=self.combat.pay_daowen_numeric_cost(name,calc,actor,
                dragon_heart_use=params.get("dragon_heart_use",0),
                cost_share_target_ref=params.get("cost_share_target_ref",""))
            self.combat.consume_tamper(actor, tamper_rule)
            actor.mark_daowen_used(name)
            if self.state.in_final_duel and not self._duel_side_can_act("player_side") and not self._duel_side_can_act("opponent_side"):
                self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
            note_action_targets(self.state, actor, _dw_selected)
            return {
                "success": True,
                "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
                "calculation": calc,
                "numeric_cost":missed_numeric_cost,
                "execution": {"daowen": name, "effects": [], "dodged": dodge_log},
                "dodge": dodge_log,
                "trigger_spell_logs": trigger_logs,
                "state": self.combat._get_combat_state(),
            }

        dragon_heart_use = params.get("dragon_heart_use", 0)
        if calc.get("custom_effects") is not None:
            # 自定义道纹：效果由声明驱动，统一走 verbs 入口；目标由声明决定。
            logs = custom_resources.apply_effects(
                self.combat, calc["custom_effects"], actor=actor, target=target,
                x=x, source=name)
            execution = {"daowen": name, "effects": logs,
                         "custom": True, "x": x}
        else:
            # 【回锋刀】缺省目标的依据之一：本次发动的 [目标]（2026-11-02）。
            previous_spell_target = getattr(self.combat, "_huifeng_spell_target", None)
            self.combat._huifeng_spell_target = target
            try:
                execution = self._execute_daowen_effect(
                    name, calc, actor, target, dragon_heart_use,
                    cost_share_target_ref=params.get("cost_share_target_ref", ""),
                    aoe_targets_override=aoe_targets_override,
                )
            finally:
                self.combat._huifeng_spell_target = previous_spell_target
        if name == "交易":
            # 【交易】发现X件遗物：逐次 3 选 1，本场战斗内有效（战终清除）。
            self.state.pending_relic_discovery_rounds = max(0, int(x) - 1)
            offer = self._offer_relic_discovery("交易")
            execution.setdefault("effects", []).append(
                {"type": "relic_discovery", "rounds": int(x),
                 "choices": offer.get("choices", []), "error": offer.get("error")})
            execution["trade_discovery"] = (
                {"choices": offer["choices"],
                 "remaining": self.state.pending_relic_discovery_rounds}
                if offer.get("success") else {"error": offer.get("error")})
        self.combat.consume_tamper(actor, tamper_rule)
        if dodge_log.get("dodged_names"):
            execution = dict(execution)
            execution["dodged"] = dodge_log
        if self.state.in_final_duel and not self._duel_side_can_act("player_side") and not self._duel_side_can_act("opponent_side"):
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        note_action_targets(self.state, actor, _dw_selected)

        return {
            "success": True,
            "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
            "calculation": calc,
            "execution": execution,
            "dodge": dodge_log or None,
            "trigger_spell_logs": trigger_logs,
            "state": self.combat._get_combat_state()
        }

    @ACTION_REGISTRY.action("cast", group="daowen")
    def _action_cast(self, params: dict) -> dict:
        """施法：发动道纹或执行统一自定义施法。

        params:
            kind: "daowen" 或 "spell"（默认 "daowen"）
            kind=spell 时透传给统一的 define_spell 语义。
        """
        if "flow" in params or "huangfu_spell" in params:
            # Phase 2：cast(flow=...) = 瞬发法术（trigger=瞬发, lifecycle=instant）。
            # 独立入口，不转发给普通道纹发动或自定义施法定义。
            # 2026-10-14 审计F4：【黄符】施法（huangfu_spell）同走瞬发入口，
            # 流程文本取自受授法术本身。
            if params.get("kind") not in (None, "flow", "instant"):
                return {"success": False,
                        "error": "cast(flow=...)不能与kind=daowen/spell同时使用"}
            return self._action_cast_instant(params)
        kind = params.get("kind", "daowen")
        sub = {k: v for k, v in params.items() if k != "kind"}
        if kind == "daowen":
            return self._action_use_daowen(sub)
        if kind == "spell":
            return self._action_define_spell(sub)
        return {"success": False, "error": f"cast.kind 必须是 daowen 或 spell，收到 {kind!r}"}

    @ACTION_REGISTRY.action("define_spell", group="daowen")
    def _action_define_spell(self, params: dict) -> dict:
        """战斗中自定义法术：持有所需道纹即可，消耗 1 次主动出手，立即生效。

        法术不属于学习行动，统一在战斗中自定义。三大法则由 DSL 解析器硬性把关
        （parse_spell_definition + 引用的道纹必须全部列入 required_daowen），
        因此不再额外要求 DM 审核中断，避免战斗中卡在待裁定队列里无法继续。
        """
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        if self.state.phase != GamePhase.IN_COMBAT.value:
            return {"success": False,
                    "error": "自定义法术只能在战斗中进行（消耗1次主动出手）；局外入口已取消"}
        if not player.is_alive:
            return {"success": False, "error": "轮回者已死亡，无法自定义法术"}
        definition = params.get("spell")
        predefined_name = params.get("spell_name")
        if definition is None and isinstance(predefined_name, str):
            predefined = self.combat.spell_definition(player, predefined_name)
            if predefined is None:
                return {"success": False, "error": f"法术大全中不存在或当前不可用：{predefined_name}"}
            if any(sp.name == predefined_name for sp in player.spells):
                return {"success": False, "error": f"法术【{predefined_name}】已经定义"}
            definition = predefined.to_dict()
            definition["lifecycle"] = params.get("lifecycle", "battle")
        if not isinstance(definition, dict):
            return {"success": False, "error": "自定义施法必须提交spell对象或spell_name"}
        built = self._build_custom_spell(player, definition, allow_predefined=predefined_name is not None)
        if "error" in built:
            return {"success": False, "error": built["error"]}
        # 定义是一个主动行动，固定1手；效果步骤在实际施法时逐步支付，
        # 自动触发不再额外收费。阶级仍用于定义约束，不再是预付价格。
        tier = built["spell"].rank
        budget_error = self._consume_actions_or_error(
            player, 1, action_type="define_spell")
        if budget_error:
            return budget_error
        self.combat.complete_spell(player, built["spell"])
        parsed = built["parsed"]
        wired = parsed.trigger in self.combat._WIRED_TRIGGERS
        result = {"spell": built["spell"].to_dict(), "wired": wired,
                  "tier": tier, "cost": "定义1次主动出手；触发不额外收费"}
        if not wired:
            result["warning"] = (f"触发时机【{parsed.trigger}】已通过句式校验，"
                                 "但该时机暂未接入战斗结算管线，本法术目前不会实际触发")
        return {"success": True, "action": "自定义法术", "result": result}

    @ACTION_REGISTRY.action("undefine_spell", group="daowen")
    def _action_undefine_spell(self, params: dict) -> dict:
        """移除一个法术定义（不花出手）。

        - 自创法术：从 entity.spells 移除（battle 作用域法术本来就会在战终被
          引擎自动清除；本入口用于战斗中主动放弃，或移除 permanent 定义）；
        - 自定义法术：移除当前定义；
        - 瞬发法术不产生绑定，因此不在此列。
        """
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        spell_name = params.get("spell_name", "")
        if not isinstance(spell_name, str) or not spell_name.strip():
            return {"success": False, "error": "必须提交spell_name"}
        spell_name = spell_name.strip()
        known_custom = any(sp.name == spell_name for sp in player.spells)
        if not known_custom:
            return {"success": False, "error": f"玩家当前没有自定义法术【{spell_name}】的定义"}
        removed = self.combat.undefine_spell(player, spell_name)
        return {"success": True, "action": f"移除法术【{spell_name}】",
                "result": {"spell_name": spell_name, "removed": removed,
                           "cost": "无（移除不花出手）",
                           "remaining_bindings": self.combat.spell_bindings(player)}}

    @ACTION_REGISTRY.action("use_resonance", group="daowen")
    def _action_use_resonance(self, params: dict) -> dict:
        """使用残韵。actor_ref 指定施法者（默认 player:0），使守擂者等轮回者也能用自己
        的残韵转化敌人道纹并让自己获得转化后道纹——与挑战者共用同一套机制。"""
        source = params.get("source_daowen", "")
        rtype = params.get("resonance_type", "")
        actor_ref = params.get("actor_ref", "player:0")
        refs = self.combat._combat_entity_refs()
        actor = refs.get(actor_ref)
        if actor is None:
            return {"success": False, "error": f"找不到actor_ref: {actor_ref}"}
        # 施法者残韵库存：玩家侧兼容 State.resonance；守擂者等非玩家轮回者严格用
        # 自己实体级的 resonance（真实准备量，无则 0）。绝不回落借他人的库存——
        # 「有就有，没有就没有」，不给没准备的对手塞残韵。
        if actor is self.state.player:
            stock = self.state.resonance
        else:
            stock = getattr(actor, "resonance", None) or {}
        if not isinstance(stock, dict) or stock.get(rtype, 0) <= 0:
            return {"success": False, "error": f"没有可用的{rtype}残韵（当前：{stock}）"}

        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        holder, holder_err = self._find_resonance_holder(source, params.get("target_ref", "") or "")
        if holder_err:
            return {"success": False, "error": holder_err}

        caster_has = holder is actor

        # 路径双向后，同一（源道纹, 残韵类型）可能通向两个相邻节点
        # （例：【杀伐】的【反转】通向【再生】与【封印】）。歧义时不替发动者挑，
        # 必须显式提交 target_daowen；未指定则原样拒绝并列出候选（残韵不消耗）。
        target_daowen = params.get("target_daowen", "") or ""

        result = ResonanceEngine.apply_resonance(
            source, rtype,
            caster_has_daowen=caster_has,
            target_has_daowen=True,
            resonance_stock=stock,  # 传入施法者残韵库存用于校验
            target_daowen=target_daowen,
        )

        if not result["success"]:
            return {"success": False, "error": result["error"]}

        capacity_error = self._conversion_capacity_error(holder, source, result["target"])
        if capacity_error:
            return {"success": False, "error": capacity_error}

        # 路径与持有者均已确认，此时才消耗（规则1：未生效不消耗）
        stock[rtype] = stock[rtype] - 1

        dest = result["target"]
        holder_changed = self._permanently_convert_daowen(holder, source, dest)
        granted = self._grant_transformed_daowen(actor, dest)
        redemption = None
        if holder.entity_type == "怪物":
            redemption = self.combat.check_redemption(holder)

        second = params.get("second_target_ref", "")
        second_source_daowen = params.get("second_source_daowen", "")
        second_log = None
        if second and any(r.name == "同魂笔" and getattr(r, "enabled", True) for r in self.state.relics):
            if not second_source_daowen:
                second_log = "同魂笔：必须指定second_source_daowen(第二个目标身上要受影响的道纹)，未生效"
            else:
                second_entity = self.combat._combat_entity_refs().get(second)
                if second_entity is None:
                    second_log = f"同魂笔：找不到目标引用{second}，未生效"
                elif second_source_daowen not in second_entity.dao_wen:
                    second_log = f"同魂笔：{second}未持有{second_source_daowen}，未生效"
                else:
                    r2 = ResonanceEngine.apply_resonance(
                        second_source_daowen, rtype,
                        caster_has_daowen=(second_source_daowen in actor.dao_wen),
                        target_has_daowen=True,
                        target_daowen=params.get("second_target_daowen", "") or "")
                    if r2.get("success"):
                        capacity_error = self._conversion_capacity_error(
                            second_entity, second_source_daowen, r2["target"])
                        if capacity_error:
                            r2 = {"success": False, "error": capacity_error}
                    if r2.get("success"):
                        new_name = r2["target"]
                        self._permanently_convert_daowen(second_entity, second_source_daowen, new_name)
                        if self._grant_transformed_daowen(actor, new_name):
                            second_log = f"同魂笔：{second_entity.name}的{second_source_daowen}永久变为{new_name}，施法者同时永久获得{new_name}"
                        else:
                            second_log = f"同魂笔：{second_entity.name}的{second_source_daowen}永久变为{new_name}；施法者已持有{new_name}"
                        if second_entity.entity_type == "怪物" and not redemption:
                            redemption = self.combat.check_redemption(second_entity)
                    else:
                        second_log = f"同魂笔：{r2.get('error', '未知原因')}，未生效"
        payload = {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {result['target']}",
            "result": result,
            "holder": holder.name,
            "holder_is_player": caster_has,
            "holder_changed": holder_changed,
            "original_preserved": dest != source and source in holder.dao_wen,
            "granted_daowen": dest if granted else None,
            "actor": actor.name,
            "second_target_log": second_log,
            "resonance_remaining": dict(stock)
        }
        if redemption:
            payload["redemption"] = redemption
        return payload

    @ACTION_REGISTRY.action("assign_learned_daowen", group="daowen")
    def _action_assign_learned_daowen(self, params: dict) -> dict:
        """把暂存的学习成果分配给现在或后来结识的朋友/员工/妖怪。"""
        if self.state.phase != GamePhase.PRE_BATTLE.value:
            return {"success": False, "error": "待分配道纹只能在局外分配"}
        name = params.get("name")
        recipient = str(params.get("recipient", params.get("target", "")) or "")
        pending = getattr(self.state, "pending_learned_daowen", [])
        if not isinstance(name, str) or name not in pending:
            return {"success": False, "error": f"待分配池中没有道纹【{name}】"}
        candidates = ([self.state.player] if self.state.player else [])
        candidates += self.state.friends + self.state.employees + self.state.temp_friends
        target = next((entity for entity in candidates if entity and entity.is_alive
                       and recipient in (entity.name, entity.runtime_id)), None)
        if target is None:
            return {"success": False, "error": f"接收者【{recipient}】不存在或已离场"}
        if name in target.dao_wen:
            return {"success": False, "error": f"【{target.name}】已经拥有道纹【{name}】"}
        if len(target.dao_wen) >= target.daowen_capacity:
            return {"success": False,
                    "error": f"【{target.name}】死者之页承载已满，当前道纹额度{target.daowen_capacity}种"}
        target.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        pending.remove(name)
        return {"success": True, "action": "分配学习道纹",
                "result": {"daowen": name, "recipient": target.name,
                           "remaining_pending_daowen": list(pending)}}

    @ACTION_REGISTRY.action("choose_diandang_daowen", group="daowen")
    def _action_choose_diandang_daowen(self, params: dict) -> dict:
        """领取【典当】发现：从当前第一组3选1里选1种获得；不消耗出手。

        领取时重验学习口径（发现之后获得的道纹可能让候选过期）：
        本组全部过期→本组作废；所选不在本组→报错不消耗；承载已满→报错不消耗。
        战斗内/局外均可领取（无阶段门禁，避免待领卡死后续流程）。
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        queue = self.state.pending_diandang_discoveries
        if not queue:
            return {"success": False, "error": "当前没有待领取的【典当】发现"}
        group = list(queue[0])
        valid = [n for n in group
                 if learnability_error(n, owned=set(player.dao_wen),
                                       region=self.state.current_region,
                                       known=DaoWenEngine.list_all()) is None]
        if not valid:
            queue.pop(0)
            return {"success": True, "action": "领取典当发现",
                    "result": {"discarded_group": group,
                               "remaining_groups": len(queue),
                               "note": "本组候选已全部过期（发现后已获得），本组作废"}}
        choice = params.get("daowen_name", "")
        if choice not in valid:
            return {"success": False, "error": "只能选择本组发现列出的道纹",
                    "choices": valid}
        cap = player.daowen_capacity
        if len(player.dao_wen) >= cap:
            return {"success": False,
                    "error": (f"死者之页承载不足：当前最多允许{cap}种道纹"
                              f"（四类共用{player.load_capacity}张死者之页），已持有{len(player.dao_wen)}种；"
                              f"本组发现保留，腾出额度后可重试")}
        player.dao_wen[choice] = DaoWenInstance(DaoWen(
            name=choice, formula="", cost_type="消耗",
            cost_formula="X", effect_formula=""))
        queue.pop(0)
        return {"success": True, "action": "领取典当发现",
                "result": {"granted": choice, "from_group": group,
                           "remaining_groups": len(queue),
                           "player_daowen": list(player.dao_wen)}}

    @ACTION_REGISTRY.action("lianxin_in_battle", group="daowen")
    def _action_lianxin_in_battle(self, params: dict) -> dict:
        """炼心（战斗中版）：不消耗出手，改为下一次局外行动额外多消耗1点精力"""
        self.state.pending_lianxin = True
        self.state.pending_energy_penalty += 1
        return {
            "success": True,
            "action": "炼心(战斗中)",
            "result": {
                "pending_lianxin": True,
                "pending_energy_penalty": self.state.pending_energy_penalty,
                "instruction": "不消耗本回合出手；下一次局外行动将额外多消耗1点精力",
            }
        }

    # 2026-09-28：_pre_battle_wangyou 已删除（局外【忘忧】被删除，忘忧香效果为溢出回复转格挡）。
