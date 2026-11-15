"""终音法器与龙族行动：决斗遗物、终音遗物、第一拥抱、黑卡、罪库、教父转轮、共享龙心、赴约北 toll、龙征、吞食、断尾、赤卒、真视之眼、血宴。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations



import math

from ..action_registry import ACTION_REGISTRY
from ..consumables import WING_EMBER, make_wing_item
from ..dm_rulings import Interrupt
from ..enums import EffectPolarity, EffectScope, InterruptType
from ..events import TWISTED_TOOL_LIBRARY
from ..models import DaoWen, DaoWenInstance
from ..yaoguai import BANNER_NAME
from .duel import handle_activate_duel_relic, handle_resolve_final_duel


class EndgameHandlersMixin:
    """宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("activate_duel_relic", group="endgame")
    def _action_activate_duel_relic(self, params: dict) -> dict:
        return handle_activate_duel_relic(self, params)

    @ACTION_REGISTRY.action("resolve_final_duel", group="endgame")
    def _action_resolve_final_duel(self, params: dict) -> dict:
        return handle_resolve_final_duel(self, params)

    @ACTION_REGISTRY.action("choose_terminal_artifact", group="endgame")
    def _action_choose_terminal_artifact(self, params: dict) -> dict:
        """领取死斗胜利后的终音法器；若选择"猩红尖牙"则先触发初拥之夜，之后才真正完整封存"""
        region = self.state.pending_terminal_region
        if not region:
            return {"success": False, "error": "当前没有待领取的终音法器"}
        options = self.TERMINAL_ARTIFACTS.get(region, [])
        choice = params.get("choice", 0)
        if not isinstance(choice, int) or not (1 <= choice <= len(options)):
            return {"success": False, "error": f"choice必须是1~{len(options)}之间的整数"}
        name, effect = options[choice - 1]
        self.state.artifacts_owned.append(name)
        self.state.pending_terminal_region = ""

        if name == "红头绳":
            self.state.has_sacrifice_action = True
        if name == "猩红尖牙":
            self.state.pending_first_embrace = True
            self.state.seal_pending_after_embrace = True
            return {"success": True, "action": "领取终音法器",
                    "result": {"artifact": name, "effect": effect, "first_embrace_pending": True,
                               "instruction": "已强制触发【初拥之夜】，请调用 choose_first_embrace(choice=1~9) 选择"}}

        seal = self._finalize_victory_seal(advance_tier=self.state.duel_tier or None)
        return {"success": True, "action": "领取终音法器",
                "result": {"artifact": name, "effect": effect, "seal": seal}}

    @ACTION_REGISTRY.action("choose_first_embrace", group="endgame")
    def _action_choose_first_embrace(self, params: dict) -> dict:
        """
        初拥之夜：9选1。1~8每项限选1次(除9「封存血脉」外)，选择后[回复]30%[血限]。
        猩红尖牙触发的这一次选择完成后，若仍处于死斗胜利流程中，会紧接着完整封存角色。
        """
        if not self.state.pending_first_embrace:
            return {"success": False, "error": "当前没有待处理的初拥之夜"}
        choice = params.get("choice", 0)
        if choice not in self.FIRST_EMBRACE_OPTIONS:
            return {"success": False, "error": "choice必须是1~9之间的整数"}
        name, effect = self.FIRST_EMBRACE_OPTIONS[choice]
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        if choice != self.FIRST_EMBRACE_RETAIN_CHOICE:
            already_chosen = name in self.state.first_embrace_traits
            if name == "真理":
                # 兼容迁移前仍以遗物保存的旧档，及任何已持有同名道纹的状态。
                already_chosen = (already_chosen or "真理" in player.dao_wen
                                  or any(relic.name == "真理眼" for relic in self.state.relics))
            if already_chosen:
                return {"success": False, "error": f"{name}已经选过，1~8每项限选1次"}

        if choice != self.FIRST_EMBRACE_RETAIN_CHOICE:
            if name == "真理":
                # 事件专属道纹由初拥直接授予；不进入普通学习/残韵流程。
                player.dao_wen[name] = self._new_truth_daowen_instance()
            elif choice in self.FIRST_EMBRACE_CONSUMABLES:
                # 选项3：发放消耗品【血翼】，不占遗物位、不入遗物视图。
                self.state.consumables.append(
                    make_wing_item(self.FIRST_EMBRACE_CONSUMABLES[choice]))
            else:
                # 其余初拥项目仍以遗物形式存放，继承遗物通用规则。
                self.state.grant_relic(name, effect, tag="血族")
            self.state.pending_first_embrace = False
            if name == "不朽之躯":
                player.blood_limit = math.ceil(player.blood_limit / 2)
                # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
                if not self.state.hp_overflow_exempt(player):
                    player.current_hp = min(player.current_hp, player.blood_limit)
        else:
            pass  # 封存血脉：不清空pending_first_embrace，保留触发权

        heal_amount = math.ceil(player.blood_limit * 0.3)
        heal_detail = self.state.apply_heal(player, heal_amount, ctx={
            "timing": "pre_battle_event", "source": "初拥之夜", "source_type": "event",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "first_embrace",
            "amount": heal_amount, "tags": {"event"},
        })

        result = {"success": True, "action": "初拥之夜",
                  "result": {"choice": choice, "trait": name, "effect": effect,
                             "healed": heal_detail["actual_heal"]}}

        if self.state.seal_pending_after_embrace:
            self.state.seal_pending_after_embrace = False
            result["result"]["seal"] = self._finalize_victory_seal(advance_tier=self.state.duel_tier or None)
        return result

    # ==================== 初拥之夜：可主动发动的具体能力 ====================

    @ACTION_REGISTRY.action("use_black_card", group="endgame")
    def _action_use_black_card(self, params: dict) -> dict:
        """黑金名片(罪孽都市终音)：[战始]可使所有敌方[目标][血限]减半，付出等量[碎片](允许负债，负债≤50)"""
        if "黑金名片" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有黑金名片"}
        if self.state.event_modifiers.get("black_card_used_battle") == self.state.current_battle:
            return {"success": False, "error": "黑金名片本场战始已经发动"}
        enemies = self.state.get_all_enemy_side()
        if not enemies:
            return {"success": False, "error": "没有可生效的敌方目标"}
        total_cost = sum(math.ceil(e.blood_limit / 2) for e in enemies)
        if self.state.shards - total_cost < -50:
            return {"success": False, "error": f"负债不能超过50(需要付出{total_cost}，当前{self.state.shards})"}
        halved = []
        for e in enemies:
            half = math.ceil(e.blood_limit / 2)
            self.state.apply_scoped_delta(
                e, "blood_limit", -half,
                scope=EffectScope.BATTLE.value, polarity=EffectPolarity.DEBUFF.value,
                source="黑金名片")
            e.current_hp = min(e.current_hp, e.blood_limit)
            halved.append({"name": e.name, "new_blood_limit": e.blood_limit})
        self.state.shards -= total_cost
        self.state.event_modifiers["black_card_used_battle"] = self.state.current_battle
        self.combat._on_cost_paid(self.state.player)
        return {"success": True, "action": "黑金名片",
                "result": {"cost": total_cost, "shards": self.state.shards, "halved": halved}}

    @ACTION_REGISTRY.action("use_crime_vault", group="endgame")
    def _action_use_crime_vault(self, params: dict) -> dict:
        """罪业金库(罪孽都市终音)：[回始]可消耗X点[碎片](X≤2%当前碎片)，获得2X点格挡"""
        if "罪业金库" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有罪业金库"}
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        target_round = self.state.current_round + 1
        if self.state.event_modifiers.get("crime_vault_used_round") == target_round:
            return {"success": False, "error": "罪业金库本回始已经发动"}
        x = params.get("x", 0)
        cap = math.floor(self.state.shards * 0.02)
        if not isinstance(x, int) or x < 1 or x > cap:
            return {"success": False, "error": f"X必须是1~{cap}(当前碎片{self.state.shards}的2%)之间的整数"}
        self.state.shards -= x
        player.gain_shield(2 * x)
        self.state.event_modifiers["crime_vault_used_round"] = target_round
        return {"success": True, "action": "罪业金库",
                "result": {"spent": x, "shield_gained": 2 * x, "shards": self.state.shards, "shield": player.shield}}

    @ACTION_REGISTRY.action("fire_godfather_revolver", group="endgame")
    def _action_fire_godfather_revolver(self, params: dict) -> dict:
        """教父左轮(罪孽都市终音)：每场 6 发，[战始]回满；
        对[目标]打出 30%自身[血限]×本场使用次数 的【必中】伤害。

        它是法器，不再借消耗品的耐久记账——回满属于[战始]重置，
        发射次数由 `state.godfather_revolver_uses` 单点计数。
        """
        if "教父左轮" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有教父左轮"}
        if self.state.godfather_revolver_uses >= self.GODFATHER_REVOLVER_AMMO:
            return {"success": False, "error": "教父左轮本场弹药已打空，下一场[战始]回满"}
        player = self.state.player
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not player or target is None:
            return {"success": False, "error": "target_ref不是当前合法目标"}
        self.state.godfather_revolver_uses += 1
        damage = math.ceil(player.blood_limit * 0.3) * self.state.godfather_revolver_uses
        dmg = self.combat._apply_hostile_damage(target, damage, "必中", player, ctx={
            "timing": self.state.combat_subphase or self.state.phase, "source": "教父左轮", "source_type": "artifact",
            "actor": player, "target": target, "mechanic": "damage", "subtype": "artifact",
            "amount": damage, "tags": {"artifact", "must_hit"},
        })
        return {"success": True, "action": "教父左轮",
                "result": {"target": target.name, "damage": damage,
                           "uses_this_battle": self.state.godfather_revolver_uses,
                           "ammo_remaining": self.GODFATHER_REVOLVER_AMMO - self.state.godfather_revolver_uses,
                           **dmg}}

    @ACTION_REGISTRY.action("select_shared_dragon_heart", group="endgame")
    def _action_select_shared_dragon_heart(self, params: dict) -> dict:
        """共心环(龙心谷终音)：[战始]选定自身拥有的一枚【××龙心】类型，本场自身/朋友/员工均可用它抵消同类型代价"""
        if "共心环" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有共心环"}
        if self.state.shared_dragon_heart_type:
            return {"success": False, "error": "共心环本场战始已经选择龙心"}
        heart_type = params.get("dragon_heart_type", "")
        heart = next((c for c in self.state.consumables
                      if c.kind == "dragon_heart" and c.dragon_heart_type == heart_type), None)
        if heart is None:
            return {"success": False, "error": f"自身没有持有{heart_type}龙心"}
        self.state.shared_dragon_heart_type = heart_type
        return {"success": True, "action": "共心环",
                "result": {"shared_dragon_heart_type": heart_type}}

    @ACTION_REGISTRY.action("declare_fuyuebei_toll", group="endgame")
    def _action_declare_fuyuebei_toll(self, params: dict) -> dict:
        """负岳碑(龙心谷终音)：预先声明"下次该[朋友]/[员工]即将撤退时，改为玩家支付生命20取消撤退与本次伤害" """
        if "负岳碑" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有负岳碑"}
        ally = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if ally is None or ally not in self.state.friends + self.state.employees:
            return {"success": False, "error": "target_ref不是朋友或员工"}
        target_ref = params["target_ref"]
        cost_share_ref = params.get("cost_share_target_ref", "")
        self.combat.validate_numeric_cost(
            self.state.player, "生命", 20, cost_share_ref)
        if target_ref not in self.state.fuyuebei_declared:
            self.state.fuyuebei_declared.append(target_ref)
        self.state.event_modifiers.setdefault("fuyuebei_cost_share_refs", {})[target_ref] = cost_share_ref
        return {"success": True, "action": "负岳碑·预声明保护",
                "result": {"protected": ally.name, "declared": list(self.state.fuyuebei_declared)}}

    # ==================== 真龙之心：代价累积与 8 种龙族遗物 ====================
    # 废除「龙性」货币。玩家实际支付的数值型【代价】自动记入
    # `state.dragon_cost_points`（只作兑换门槛，不可花用、不可转账），每满 12 点
    # 换 1 件未持有的龙族遗物。道纹/法术/事件里的代价本来就支持多途径支付，
    # 这里不再需要「先换龙性再花龙性」的中间层。

    # 烬翼改为消耗品（耐久5/5），不再是龙族遗物；
    # 兑换仍走真龙之心的 12 点代价累积，只是发的是消耗品而不是遗物。
    DRAGON_TRAITS = ["龙族血脉", "龙威", "龙族利爪", "龙息", "震岳龙躯", "吞骸龙胃", "断尾求生"]
    DRAGON_CONSUMABLE_TRAITS = {WING_EMBER: 5}
    DRAGON_TRAIT_EFFECTS = {
        "龙族血脉": "对怪物造成伤害后，直接使其［命零］；对非怪物造成伤害翻倍",
        "龙威": "［战始］起，所有敌方必须优先选择你为[目标]",
        "龙族利爪": "［战始］起，你的[攻击力]等同当前法力×2",
        "龙息": "所有敌方[目标]行动前，受到10×当前回合数的必中伤害",
        "震岳龙躯": "你受到的单次伤害超过15点时，超出部分无效",
        "吞骸龙胃": "任意怪物［命零］后，可将其吞噬：自身获得[回复12]，并选择一枚【××龙心】，使其当前耐久+6",
        "断尾求生": "自身即将［命零］时，可销毁一件本场获得的其他龙族遗物，抵消本次使自身［命零］的伤害",
    }
    # 龙威通过prepare_monster_phase收窄合法目标列表实现；resolve只接受prepare列出的目标。
    DRAGON_COST_PER_TRAIT = 12

    @ACTION_REGISTRY.action("unlock_dragon_trait", group="endgame")
    def _action_unlock_dragon_trait(self, params: dict) -> dict:
        """真龙之心：每满 12 点代价累积，获得 1 件未持有的龙族遗物/翼类消耗品。"""
        if "真龙之心" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有真龙之心"}
        trait = params.get("trait", "")
        options = self.DRAGON_TRAITS + list(self.DRAGON_CONSUMABLE_TRAITS)
        if trait not in options:
            return {"success": False,
                    "error": f"trait必须是{options}之一，收到{trait!r}"}
        if trait in self.state.dragon_traits:
            return {"success": False, "error": f"已持有龙族遗物【{trait}】"}
        if trait in self.DRAGON_CONSUMABLE_TRAITS and any(
                c.name == trait for c in self.state.consumables):
            return {"success": False, "error": f"已持有【{trait}】（消耗品，耐久已满）"}
        if self.state.dragon_cost_points < self.DRAGON_COST_PER_TRAIT:
            return {"success": False,
                    "error": (f"代价累积不足：需要{self.DRAGON_COST_PER_TRAIT}点，"
                              f"当前{self.state.dragon_cost_points}点（实际支付数值型代价即可累积）")}
        self.state.dragon_cost_points -= self.DRAGON_COST_PER_TRAIT
        if trait in self.DRAGON_CONSUMABLE_TRAITS:
            item = make_wing_item(trait)
            self.state.consumables.append(item)
            return {"success": True, "action": f"真龙之心·获得消耗品{trait}",
                    "result": {"trait": trait, "effect": item.effect, "kind": "consumable",
                               "max_uses": item.max_uses,
                               "cost_points_remaining": self.state.dragon_cost_points}}
        effect = self.DRAGON_TRAIT_EFFECTS[trait]
        self.state.grant_relic(trait, effect, tag="龙族")
        return {"success": True, "action": f"真龙之心·解锁{trait}",
                "result": {"trait": trait, "effect": effect,
                           "cost_points_remaining": self.state.dragon_cost_points}}

    @ACTION_REGISTRY.action("devour_monster", group="endgame")
    def _action_devour_monster(self, params: dict) -> dict:
        """吞骸龙胃：任意怪物[命零]后可将其吞噬，自身获得回复12，并选择一枚龙心使其耐久+6"""
        if "吞骸龙胃" not in self.state.dragon_traits:
            return {"success": False, "error": "没有吞骸龙胃"}
        monster_ref = params.get("monster_ref", "")
        monster = None
        if isinstance(monster_ref, str) and monster_ref.startswith("enemy:"):
            try:
                index = int(monster_ref.split(":", 1)[1])
                monster = self.state.enemies[index]
            except (ValueError, IndexError):
                monster = None
        if monster is None or monster.is_alive:
            return {"success": False, "error": "monster_ref不是已命零怪物"}
        player = self.state.player
        heal_detail = self.state.apply_heal(player, 12, ctx={
            "timing": self.state.combat_subphase or self.state.phase, "source": "吞骸龙胃", "source_type": "relic",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "devour_monster",
            "amount": 12, "tags": {"relic"},
        })
        heart_name = params.get("dragon_heart", "")
        heart = next((c for c in self.state.consumables if c.name == heart_name and c.kind == "dragon_heart"), None)
        if heart is not None:
            heart.current_uses += 6
            heart.max_uses += 6
        return {"success": True, "action": "吞骸龙胃",
                "result": {"healed": heal_detail["actual_heal"],
                           "dragon_heart_boosted": heart_name if heart is not None else None}}

    @ACTION_REGISTRY.action("declare_tail_sacrifice", group="endgame")
    def _action_declare_tail_sacrifice(self, params: dict) -> dict:
        """断尾求生：预先声明本次即将命零时，愿意移除哪一种本场获得的其他龙族遗物来抵消伤害"""
        if "断尾求生" not in self.state.dragon_traits:
            return {"success": False, "error": "没有断尾求生"}
        trait = params.get("trait", "")
        others = [t for t in self.state.dragon_traits if t != "断尾求生"]
        if trait not in others:
            return {"success": False, "error": f"trait必须是本场已获得的其他龙族遗物之一: {others}"}
        self.state.dragon_tail_sacrifice_declared = trait
        return {"success": True, "action": "断尾求生·预声明",
                "result": {"declared_sacrifice": trait}}

    @ACTION_REGISTRY.action("enslave_as_chizu", group="endgame")
    def _action_enslave_as_chizu(self, params: dict) -> dict:
        """血族尖牙：代价血限20，使生命低于自身的一个[目标]转化为听命于你的赤族"""
        if "血族尖牙" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有血族尖牙"}
        player = self.state.player
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not player or target is None or target not in self.state.enemies:
            return {"success": False, "error": "target_ref不是存活敌方目标"}
        if target.current_hp >= player.current_hp:
            return {"success": False, "error": "目标当前生命必须低于自身才能被转化"}
        payment = self.combat.pay_numeric_cost(
            player, "血限", 20,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            cost_context={"timing": "player_action", "source": "血族尖牙", "source_type": "relic", "tags": {"active_payment"}})
        target.entity_type = "赤族"
        target.is_chizu_of = player.name
        target.is_deployed = True
        self.state.enemies.remove(target)
        self.state.friends.append(target)
        self.state.chizu_names.append(target.name)
        return {"success": True, "action": "血族尖牙",
                "result": {"cost": payment, "enslaved": target.name,
                           "chizu_names": list(self.state.chizu_names)}}

    @ACTION_REGISTRY.action("use_truth_eye", group="endgame")
    def _action_use_truth_eye(self, params: dict) -> dict:
        """旧 action 名称兼容入口；新界面只暴露 use_daowen(daowen_name=真理)。"""
        return self._action_use_truth_daowen(params)

    @ACTION_REGISTRY.action("blood_feast", group="endgame")
    def _action_blood_feast(self, params: dict) -> dict:
        """血食：可使一名听命于你的赤族[命零]，自身获得等同于该赤族当前生命的[回复]"""
        if "血食" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有血食"}
        player = self.state.player
        chizu = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if (not player or chizu is None or chizu not in self.state.friends
                or chizu.entity_type != "赤族" or not chizu.is_alive):
            return {"success": False, "error": "target_ref不是存活赤族"}
        amount = chizu.current_hp
        feast_ctx = {
            "timing": self.state.combat_subphase or self.state.phase, "source": "血食",
            "source_type": "relic", "actor": player, "target": chizu, "owner": player,
            "mechanic": "death", "subtype": "blood_feast", "amount": amount,
            "tags": {"relic", "sacrifice"},
        }
        chizu.current_hp = 0
        # 修复：此前直接写 is_alive=False，赤族之死不产生 _death_ctx、不触发任何[命零]效果。
        self.combat._check_hp_zero_death(chizu, ctx=feast_ctx)
        heal_detail = self.state.apply_heal(player, amount, ctx={
            "timing": self.state.combat_subphase or self.state.phase, "source": "血食", "source_type": "relic",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "blood_feast",
            "amount": amount, "tags": {"relic"},
        })
        return {"success": True, "action": "血食",
                "result": {"sacrificed": chizu.name, "healed": heal_detail["actual_heal"], "player_hp": player.current_hp}}

    # ==================== 自定义资源：声明 / 激活 / 列表 ====================

    # 乱葬岗「亡者的执念」三选一法器（副本/乱葬岗.md）。
    # 该副本终音尚未接入引擎：三件都只登记名字与效果，不发放。
    DRAFT_ARTIFACTS = ("染血嫁衣", "燃魂灯", BANNER_NAME)


    # ==================== 终音法器：可主动发动的具体效果 ====================
