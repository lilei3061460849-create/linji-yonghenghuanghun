"""枚举定义"""
from enum import Enum, auto


class GamePhase(Enum):
    """游戏阶段"""
    SETUP = "setup"                    # 开局分配
    PRE_BATTLE = "pre_battle"          # 局外阶段
    BATTLE_START = "battle_start"      # 战始
    IN_COMBAT = "in_combat"            # 战斗回合中
    BATTLE_END = "battle_end"          # 战终
    DEAD_DUEL = "dead_duel"            # 最终死斗
    GAME_OVER = "game_over"


class CombatSubphase(Enum):
    """一场战斗内的强制结算顺序。"""
    AWAIT_ROUND_START = "await_round_start"
    PLAYER_ACTIONS = "player_actions"
    MONSTER_ACTIONS = "monster_actions"
    AWAIT_ROUND_END = "await_round_end"


class ActionPhase(Enum):
    """回合内结算阶段"""
    BEFORE_DAMAGE_TAKEN = "受到伤害前"
    DAMAGE_MITIGATION = "抵消伤害时"
    DODGE = "闪避攻击"
    AFTER_DAMAGE_TAKEN = "受到伤害后"
    BEFORE_LIFE_LOST = "失去生命前"
    AFTER_LIFE_LOST = "失去生命后"


class TriggerTiming(Enum):
    """触发时点"""
    BATTLE_START = "战始"
    BATTLE_END = "战终"
    ROUND_START = "回始"
    ROUND_END = "回终"
    SELF_TURN_END = "自身回合结束"
    ENEMY_ROUND_START = "敌回始"
    ENEMY_ROUND_END = "敌回终"
    ON_TARGET = "目标选定"
    BEFORE_DAMAGE = "受到伤害前"
    AFTER_DAMAGE = "受到伤害后"
    AFTER_LIFE_LOST = "失去生命后"
    BEFORE_LIFE_LOST = "失去生命前"
    ALWAYS = "常驻"


class EffectScope(Enum):
    """状态变化的生命周期；数值正负与生命周期相互独立。"""
    ROUND = "round"            # 当前回合结束时回滚
    BATTLE = "battle"          # 当前战斗结束时回滚
    RUN = "run"                # 本次轮回持续
    PERMANENT = "permanent"    # 跨轮回永久
    COST = "cost"              # 代价后果，不因清除增益/减益而回滚


class EffectPolarity(Enum):
    """效果极性仅供规则/界面识别，不决定何时清除。"""
    BUFF = "buff"
    DEBUFF = "debuff"
    NEUTRAL = "neutral"


class CostType(Enum):
    """代价类型"""
    MANA = "消耗"           # 法力消耗
    BLEED = "流血"          # 失去X点生命
    AGING = "衰老"          # 失去X点血限
    EXHAUST = "枯竭"        # 失去X点法限
    SHRINK = "萎缩"         # 失去X点速限
    FATIGUE = "疲惫"        # 失去X点当前速度
    AMNESIA = "失忆"        # 永久失去X种道纹
    MUTATION = "异变"       # 获得X层异变(怪物)
    COOLDOWN = "冷却"       # 冷却X场
    UNIQUE = "唯一"         # 本次轮回只能用一次


class InterruptType(Enum):
    """中断类型 - 需要DM裁定"""
    WISH = "许愿"               # 轮回者向"某人"祈求，愿望以扭曲方式实现（2026-08-19 新增，替代急中生智）
    ESCAPE_AND_PURSUIT = "逃跑与追击"  # 2026-10-13 逃跑改制后不再产生新中断；保留枚举值以兼容旧存档
    DEATH_INHERITANCE = "死者之页"
    UNSEEN_SCENE = "未见场景"
    CUSTOM = "自定义"


class DamageType(Enum):
    """伤害类型"""
    NORMAL = "普通"
    IGNORE_SHIELD = "无视格挡"
    IGNORE_DODGE = "无视闪避"
    MUST_HIT = "必中"
    REFLECT = "反射"
    COST = "代价"  # 代价绝对无法被格挡吸收


class EntityType(Enum):
    """实体类型"""
    REINCARNATOR = "轮回者"
    MONSTER = "怪物"
    FRIEND = "朋友"
    EMPLOYEE = "员工"
    CREATION = "造物"
    # 兼容旧代码可能引用的枚举成员名；新实体与新文案统一使用 CREATION。
    TEMP_FRIEND = "造物"

    @classmethod
    def _missing_(cls, value):
        # 旧存档若以枚举值保存“临时朋友”，读入时归一为“造物”。
        if value == "临时朋友":
            return cls.CREATION
        return None
