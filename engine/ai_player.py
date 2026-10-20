"""
AI玩家模块
职责：
1. AI作为决策者调用游戏引擎
2. 决策前必须调用引擎获取状态，禁止自行编造数值
3. 每次行动后由校验器检查合规性
4. 遇到Interrupt时暂停，等待DM裁定

支持的AI后端（全部免费可用）：
- Google Gemini API（免费，无需信用卡）
- Groq API（免费，超快推理）
- OpenRouter API（免费模型变体）
- DeepSeek API（注册送额度）
- 占位符（开发测试用）
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional, Any, Callable
from .api import GameEngine
from .validator import RuleValidator
from .rule_sync import RuleSync
from .dm_rulings import Interrupt
from .ai_rules import (
    battle_start_relic_choices, round_start_relic_choices,
    pick_wave_dodge_targets,
    try_fire_godfather_revolver, try_select_shared_dragon_heart,
    try_use_black_card, try_use_blood_wings, try_use_crime_vault,
    try_use_dragon_wings,
)
from .ai_memory import context_for_ai, ensure_memory, remember_action


class AIDecision:
    """AI决策记录"""
    def __init__(self, action_type: str, params: dict, reasoning: str = ""):
        self.action_type = action_type
        self.params = params
        self.reasoning = reasoning
        self.timestamp = time.time()
    
    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "params": self.params,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp
        }


class AIBackend:
    """AI后端接口（抽象基类）"""
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        raise NotImplementedError

# ========== 通用OpenAI兼容调用器 ==========

def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7
) -> Optional[str]:
    """
    通用OpenAI兼容API调用
    支持：Groq, DeepSeek, OpenRouter, 任何OpenAI兼容端点
    """
    url = f"{base_url}/chat/completions"
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": temperature,
        "max_tokens": 2000
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[AI API错误] HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"[AI API错误] {type(e).__name__}: {e}")
        return None


def _call_gemini(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str
) -> Optional[str]:
    """Google Gemini API调用（免费，无需信用卡）"""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{system_prompt}\n\n---\n\n{user_prompt}"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2000,
            "responseMimeType": "application/json"
        }
    }
    
    headers = {"Content-Type": "application/json"}
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"]
    
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[Gemini错误] HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"[Gemini错误] {type(e).__name__}: {e}")
        return None


# ========== 系统提示词 ==========

SYSTEM_PROMPT = """你是第四宇宙游戏的AI玩家。你的任务是根据游戏状态做出最优决策。

核心规则：
1. 你只能从"可用行动"列表中选择（action_type 与 params_schema 是唯一权威），不能编造行动或参数
2. 所有数值计算与结算由游戏引擎完成：你不要自己算伤害/法力/层数，也不要替引擎预演结果
3. 每次决策返回JSON格式：{"action_type": "...", "params": {...}, "reasoning": "..."}
4. reasoning用一两句话说明你的决策依据
5. **没有规则AI兜底**：引擎不会在你提交失败后替你换一个动作，也不会用打分帮你
   选牌；提交被拒就读 error / instruction，自己修正后用同一token重提

决策粒度（动作槽位）：
- 每个**动作槽位**提交**一条**决策；槽位数量由引擎的当前出手预算决定（基础2次，叠加【疯狂】+X、
  【无力】-X、【蓄锐·增】+1等修正；怪物侧另有自己的预算）。不要假定任何固定的每回合次数，
  更不要把多次独立行动打包进同一条提交
- 槽位用完、或不想再出手时，提交 prepare_monster_phase 结束己方行动阶段
- 他人回合的闪避/招架/反应法术，由你在 resolve_attack / resolve_monster_phase 里逐项显式提交，
  引擎不会替你补默认值
- 招架不是主动行动：在 resolve_attack 的单击中提交 parry=true，先消耗1速度；真刀免疫伤害并返还1速度，空刀不返还
- 闪避同样逐击提交，稳定消耗1速度并取消整次判定；同一击只能选择招架、闪避、血影之一

法术（现行架构，2026-10）：
- 你决定**提交什么**，引擎决定**如何执行**：结算时按当时的真实状态逐步执行
- X阶法术使用X种不同道纹，define_spell/cast必须一次消耗X次出手；可提前【蓄锐】积攒；lifecycle默认battle
  （本场战斗有效，战终自动清除），显式 permanent 才跨战斗保留
- 瞬发法术用 cast（flow + steps）：一次出手依次发动多种已持有道纹，每一步都算一次"发动道纹"
  （照常触发敌方"目标发动道纹前"反应、照付代价），执行完不留在角色身上
  · steps 必须覆盖 flow 按深度优先顺序展开的**全部决策槽位**（if 的两个分支与循环体各计一条，
    即使本次不一定走到）；执行器只消费实际走到的槽位，数量不符会被拒绝并报出应有步数
  · 分支（若/否则）与循环都由**执行器在执行期**按真实状态求值；循环可用可选 max_iterations
    限制本轮最多跑几轮。你不要自己展开循环、不要预演分支走向、不要替引擎推演每一步结果
  · 某步付不起是**执行期中断**：已结算的步骤保留、出手不退——先算清总花费再提交
- 生命周期三档：instant（瞬发，执行完不留存）／battle（本场有效，战终清除）／
  permanent（跨战斗保留，代价与风险自担）
- 反应深度（R-1）：法术的每一步视作一次发动，可引发对方的"目标发动道纹前"反应法术，
  但反应产生的步骤不再引发新的反应（A→B 允许，A→B→A 被阻断）

战斗决策要点：
- 提交前先看自己的致死进度（迷失/癌变/凡庸）与法力，不要把动作打到阈值上自爆
- 法力是**一场战斗一池**：消耗后不会自动回填（聚能除外），发动法力道纹会同步削弱本场剩余时间的普攻
- 资源（法力/速度/碎片/代价承受力）是稀缺的，优先使用能改变局势的道纹，不要无脑输出；
  允许冒险（赌一把/低血强杀），但要清楚代价

副本默认入口：
- 任何副本选定后，默认执行 `world_initialize` 进入该副本主题的开放地图（21×21）；除非用户明确要求旧线性流程，不要直接跳过地图初始化去 `battle_start`
- 开放地图中通过 `world_move` / `world_wait` / `world_interact` 探索；只有地图遭遇生成后，才用带 `world_encounter_id` 的 `battle_start` 进入现有战斗引擎

常见 action_type（细节以"可用行动"里的 params_schema / note 为准）：
- setup_attributes / setup_choose_initial_daowen / setup_choose_resonance / setup_choose_region
- pre_battle_action（局外行动）、choose_discovered_relic / choose_discovered_item
- use_daowen：发动一次道纹（params: daowen_name, x, target_ref, dodge, blood_shadow, spell_choices）
- define_spell：统一自定义施法；法术大全条目用 spell_name，完全自定义法术用 spell；定义后立即生效
- define_spell：战斗中自创触发型法术（params: spell{name, required_daowen, trigger_condition, effect_flow, lifecycle}）
- cast：施法（带flow时=瞬发法术；params: flow, target_ref, steps[{x, target_ref?, dodge, trigger_spell_choices}], max_iterations?）
- prepare_attack / resolve_attack：两阶段攻击（先prepare拿一次性token，再逐击显式提交闪避、招架、血影与反应法术）
- declare_evolution（怪物进化·发动原初X）
- focus：聚能，1次出手→恢复50%已损法力（向上取整）
- rest：蓄锐，1次出手→下[回始]获得【蓄锐·增】（出手+1，持续1回合）
- prepare_monster_phase / resolve_monster_phase：怪物阶段两阶段接口（为每个actors条目提交完整选择）
- battle_start / round_start / round_end / battle_end：阶段推进；存在候选时必须显式提交
  relic_choices / spell_choices，不能省略（无候选时不用传）
- 其余动作（use_resonance / consume_item / command_ally / resolve_ally_phases / deploy_employee /
  法器与龙性动作等）见"可用行动"列表
"""


def _build_user_prompt(state: dict, available_actions: dict, context: str) -> str:
    return f"""当前游戏状态：
{json.dumps(state.get('state', {}), ensure_ascii=False, indent=2)}

可用行动：
{json.dumps(available_actions, ensure_ascii=False, indent=2)}

{f'上下文：{context}' if context else ''}

请做出决策，返回JSON格式。"""


def _parse_ai_response(content: str) -> Optional[AIDecision]:
    """解析AI返回的JSON"""
    if not content:
        return None
    
    try:
        # 尝试直接解析
        data = json.loads(content)
        return AIDecision(
            action_type=data.get("action_type", "noop"),
            params=data.get("params", {}),
            reasoning=data.get("reasoning", "")
        )
    except json.JSONDecodeError:
        # 尝试从markdown代码块中提取
        import re
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return AIDecision(
                    action_type=data.get("action_type", "noop"),
                    params=data.get("params", {}),
                    reasoning=data.get("reasoning", "")
                )
            except json.JSONDecodeError:
                pass
        
        # 尝试找到第一个{到最后一个}
        start = content.find('{')
        end = content.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(content[start:end+1])
                return AIDecision(
                    action_type=data.get("action_type", "noop"),
                    params=data.get("params", {}),
                    reasoning=data.get("reasoning", "")
                )
            except json.JSONDecodeError:
                pass
    
    return None


# ========== 免费AI后端 ==========

class GeminiBackend(AIBackend):
    """
    Google Gemini后端（免费，无需信用卡）
    注册地址：https://aistudio.google.com/apikey
    环境变量：GEMINI_API_KEY
    """
    
    def __init__(self, api_key: str = None, model: str = "gemini-2.0-flash"):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model
        if not self.api_key:
            raise ValueError(
                "需要设置 GEMINI_API_KEY。\n"
                "免费获取：https://aistudio.google.com/apikey"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_gemini(self.api_key, self.model, SYSTEM_PROMPT, user_prompt)
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        # 回退
        return AIDecision("noop", {}, f"[Gemini解析失败] 原始回复: {content[:200] if content else 'None'}")


class GroqBackend(AIBackend):
    """
    Groq后端（免费，超快推理）
    注册地址：https://console.groq.com/keys
    环境变量：GROQ_API_KEY
    """
    
    def __init__(self, api_key: str = None, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = model
        self.base_url = "https://api.groq.com/openai/v1"
        if not self.api_key:
            raise ValueError(
                "需要设置 GROQ_API_KEY。\n"
                "免费获取：https://console.groq.com/keys"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_openai_compatible(
            self.base_url, self.api_key, self.model, SYSTEM_PROMPT, user_prompt
        )
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        return AIDecision("noop", {}, f"[Groq解析失败] 原始回复: {content[:200] if content else 'None'}")


class OpenRouterBackend(AIBackend):
    """
    OpenRouter后端（免费模型变体）
    注册地址：https://openrouter.ai/keys
    环境变量：OPENROUTER_API_KEY
    
    免费模型（在模型名后加 :free）：
    - meta-llama/llama-4-scout:free
    - google/gemma-3-27b-it:free
    - deepseek/deepseek-r1-0528:free
    - mistralai/mistral-small-3.1-24b-instruct:free
    """
    
    def __init__(self, api_key: str = None, model: str = "meta-llama/llama-4-scout:free"):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1"
        if not self.api_key:
            raise ValueError(
                "需要设置 OPENROUTER_API_KEY。\n"
                "免费获取：https://openrouter.ai/keys\n"
                "免费模型列表：在模型名后加 :free"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_openai_compatible(
            self.base_url, self.api_key, self.model, SYSTEM_PROMPT, user_prompt
        )
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        return AIDecision("noop", {}, f"[OpenRouter解析失败] 原始回复: {content[:200] if content else 'None'}")


class DeepSeekBackend(AIBackend):
    """
    DeepSeek后端（注册送额度，性价比极高）
    注册地址：https://platform.deepseek.com/api_keys
    环境变量：DEEPSEEK_API_KEY
    """
    
    def __init__(self, api_key: str = None, model: str = "deepseek-chat"):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = model
        self.base_url = "https://api.deepseek.com/v1"
        if not self.api_key:
            raise ValueError(
                "需要设置 DEEPSEEK_API_KEY。\n"
                "免费获取：https://platform.deepseek.com/api_keys"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_openai_compatible(
            self.base_url, self.api_key, self.model, SYSTEM_PROMPT, user_prompt
        )
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        return AIDecision("noop", {}, f"[DeepSeek解析失败] 原始回复: {content[:200] if content else 'None'}")


# ---------------------------------------------------------------------------
# 选区决策：按已持有遗物与副本专属道纹的代价类型协同打分
# ---------------------------------------------------------------------------
# 2026-09-13：此前 setup_choose_region 硬编码返回"罪孽都市"，不读任何状态——
# 实测 29 个"开局拿到回锋刀/避风铃"的样本选扭曲都市 0 次（见
# sim/probe_region_choice_synergy.py）。遗物在选区之前就已确定（见
# engine/handlers/setup.py::handle_setup_choose_region 的前置校验），
# 因此这个协同在时序上完全可判。
#
# 打分不写死任何副本名或道纹名：遗物按其效果文本关心的"代价类型/资源"归类，
# 副本按其专属道纹实际使用的 cost_type 归类，两者求交集即为协同分。
# 新增遗物或改动道纹代价后，本函数自动跟随，无需同步维护表。

# 遗物 → 它能把哪类代价/资源转化为收益（读效果文本判定，不写死遗物名单）
_RELIC_SYNERGY_KEYS = (
    ("速度", ("失去1点速度", "失速", "速度归零", "当前速度")),
    ("流血", ("流血",)),
    ("碎片", ("碎片",)),
    ("残韵", ("残韵",)),
    ("异变", ("异变",)),
)


def _relic_synergy_tags(relics) -> set:
    """从遗物效果文本提取它关心的资源类型。"""
    tags = set()
    for relic in relics or []:
        text = ""
        if isinstance(relic, dict):
            text = f"{relic.get('name', '')}{relic.get('effect', '')}"
        else:
            text = f"{getattr(relic, 'name', '')}{getattr(relic, 'effect', '')}"
        for tag, needles in _RELIC_SYNERGY_KEYS:
            if any(n in text for n in needles):
                tags.add(tag)
    return tags


# 道纹 cost_type → 对应的资源标签（与遗物标签同一命名空间）
_COST_TO_TAG = {"疲惫": "速度", "流血": "流血", "碎片": "碎片",
                "假碎片": "碎片", "异变": "异变"}


def _region_cost_tags(region: str) -> set:
    """副本专属道纹实际使用的代价类型 → 资源标签。"""
    from .gamedata import REGION_EXCLUSIVE_DAOWEN
    from .daowen import DaoWenEngine
    DaoWenEngine.register_all()
    tags = set()
    for name in REGION_EXCLUSIVE_DAOWEN.get(region, ()):  # noqa: SIM118
        try:
            calc = DaoWenEngine.resolve(name, 2, target=None, caster=None)
        except Exception:
            continue
        tag = _COST_TO_TAG.get(calc.get("cost_type"))
        if tag:
            tags.add(tag)
        # 产出资源也算协同（如【透支】多代价换法力、【超频】买速度）
        if calc.get("mana_gain") or calc.get("speed_boost"):
            tags.add("速度")
    return tags


def pick_region_by_synergy(inner: dict, valid=("罪孽都市", "扭曲都市", "龙心谷", "乱葬岗")):
    """按持有遗物与各副本专属道纹的代价协同选副本。

    返回 (region, reasoning)。无任何协同时回落到既有默认"罪孽都市"，
    保持旧行为不变（不给没依据的随机漂移）。
    """
    tags = _relic_synergy_tags(inner.get("relics"))
    if not tags:
        return valid[0], "无遗物协同信息，按默认选择初始副本"
    best, best_score, best_hit = valid[0], 0, set()
    for region in valid:
        hit = tags & _region_cost_tags(region)
        if len(hit) > best_score:
            best, best_score, best_hit = region, len(hit), hit
    if best_score <= 0:
        return valid[0], "遗物与各副本专属道纹无代价协同，按默认选择初始副本"
    return best, f"遗物关注{sorted(tags)}，与【{best}】专属道纹代价{sorted(best_hit)}协同"


class PlaceholderBackend(AIBackend):
    """
    占位符后端（开发测试用，不需要API key）
    返回默认决策，不实际调用AI
    """
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        phase = state.get("state", {}).get("phase", "unknown")
        
        inner = state.get("state", {})
        if inner.get("pending_redemption"):
            return AIDecision("resolve_redemption", {"option": 2}, "默认终结救赎（等同击杀：战终按命零产碎片）")
        if phase == "setup":
            relic_choices = inner.get("pending_relic_choices") or []
            if relic_choices:
                return AIDecision("choose_discovered_relic", {
                    "relic_name": relic_choices[0],
                }, "从发现候选中选择开局遗物")
            choices = inner.get("pending_initial_daowen_choices") or []
            if choices:
                return AIDecision("setup_choose_initial_daowen", {
                    "daowen_name": choices[0],
                }, "从发现候选中选择初始道纹")
            if inner.get("player") is None:
                return AIDecision("setup_attributes", {
                    "name": "AI轮回者",
                    "blood_points": 10,
                    "speed_points": 8,
                    "mana_points": 7
                }, "默认开局分配")
            if not inner.get("resonance"):
                return AIDecision("setup_choose_resonance", {
                    "resonance_type": "转换",
                }, "选择初始残韵")
            if not inner.get("current_region"):
                region, why = pick_region_by_synergy(inner)
                return AIDecision("setup_choose_region", {
                    "region": region,
                }, why)
            return AIDecision("noop", {}, "开局步骤已完成")
        
        elif phase == "pre_battle":
            # 选定任何副本后默认走开放地图；旧线性战斗只作为显式兼容路径。
            if not inner.get("world_state") and any(
                    action.get("action_type") == "world_initialize"
                    for action in available_actions.get("actions", []) or []):
                return AIDecision("world_initialize", {"width": 21, "height": 21},
                                  "按默认模式初始化开放地图并在当前副本区域探索")
            engine = getattr(self, "engine", None)
            # 精力耗尽时，可进入战始：按共享策略显式提交可选战始遗物（可以不用但不能不让用）。
            for action in available_actions.get("actions", []) or []:
                if action.get("action_type") == "battle_start" and engine is not None:
                    return AIDecision("battle_start", {
                        "relic_choices": battle_start_relic_choices(engine),
                    }, "进入战始并提交可选遗物决策")
            return AIDecision("pre_battle_action", {
                "sub_action": "修行",
                "tier": 1
            }, "默认修行获取属性点")
        
        elif phase == "in_combat":
            pending_type = available_actions.get("action_type")
            # 可选法器（黑金名片/罪业金库/烬翼/鲜血之翼/教父左轮/共心环）：
            # 有引擎引用且对应行动在可用列表时，按策略函数尝试发动（可以不用但不能不让用）。
            # commit=False 只返回计划不执行，由 AIPlayer.play_turn 统一 execute_action。
            engine = getattr(self, "engine", None)
            actions = available_actions.get("actions", [])
            if engine is not None:
                artifact_map = {
                    "select_shared_dragon_heart": try_select_shared_dragon_heart,
                    "use_black_card": try_use_black_card,
                    "use_crime_vault": try_use_crime_vault,
                    "use_dragon_wings": try_use_dragon_wings,
                    "use_blood_wings": try_use_blood_wings,
                    "fire_godfather_revolver": try_fire_godfather_revolver,
                }
                for action in actions:
                    fn = artifact_map.get(action.get("action_type"))
                    if fn is None:
                        continue
                    plan = fn(engine, commit=False)
                    if plan and plan.get("_plan"):
                        return AIDecision(plan["action_type"], plan["params"],
                                          "发动可选法器")
            if pending_type == "resolve_attack":
                options = available_actions.get("target_options", [])
                target = options[0] if options else None
                hits = [] if target is None else [{
                    "target_ref": target["ref"], "dodge": False, "blood_shadow": False,
                    "spell_choices": {timing: {spell["spell_name"]: {"use": False}
                                               for spell in target.get("spell_options", {}).get(timing, [])}
                                      for timing in ("before", "after", "damage_after", "life_before")},
                } for _ in range(available_actions.get("hit_count", 0))]
                return AIDecision("resolve_attack", {"token": available_actions["token"], "hits": hits},
                                  "提交完整攻击选择")
            if pending_type == "resolve_monster_phase":
                choices = []
                for actor in available_actions.get("actors", []):
                    dao = None
                    action_count = actor["base_attack_actions"]
                    hit_count = actor["base_hits_per_attack"]
                    if actor["daowen_options"]:
                        option = actor["daowen_options"][0]
                        dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                               "trigger_spell_choices": {
                                   holder: {spell["spell_name"]: {"use": False} for spell in spells}
                                   for holder, spells in option.get("trigger_spell_options", {}).items()}}
                        if option["requires_target"]: dao["target_ref"] = option["target_options"][0]["ref"]
                        if option["dodge_submission"] == "per_target":
                            # 波及X：必须恰好提交X个目标（候选全量提交会在候选>X时被拒）。
                            dao["dodge_targets"] = pick_wave_dodge_targets(option)
                        if option["resolves_as"] == "疯狂": action_count += option["x"]
                    target = (actor["attack_target_options"][0]
                              if actor["attack_target_options"] else None)
                    if target is None:
                        # 无合法攻击目标：本回合不出手（引擎prepare已置base_attack_actions=0）
                        attacks = []
                    else:
                        spell_choices = {timing: {spell["spell_name"]: {"use": False}
                                                  for spell in target.get("spell_options", {}).get(timing, [])}
                                         for timing in ("before", "after", "damage_after", "life_before")}
                        attacks = [{"hits": [{"target_ref": target["ref"], "dodge": False,
                                               "blood_shadow": False, "spell_choices": spell_choices}
                                              for _ in range(hit_count)]}
                                   for _ in range(action_count)]
                    choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                                    "attack_actions": attacks})
                return AIDecision("resolve_monster_phase",
                                  {"token": available_actions["token"], "choices": choices},
                                  "提交完整怪物阶段选择")
            actions = available_actions.get("actions", [])
            for action in actions:
                action_type = action.get("action_type")
                if action_type in ("round_start", "round_end", "prepare_monster_phase"):
                    round_params = {}
                    if action_type == "round_start":
                        engine = getattr(self, "engine", None)
                        if engine is not None:
                            choices = round_start_relic_choices(engine)
                        else:
                            schema = action.get("params_schema", {}).get("relic_choices", {})
                            choices = {name: {"use": False}
                                       for name in schema if name != "_instruction"}
                        round_params = {"relic_choices": choices}
                    return AIDecision(action_type, round_params,
                                      "推进合法战斗子阶段")
                if action_type == "use_daowen" and action.get("available", True):
                    schema = action["params_schema"]
                    enemies = [target for target in schema.get("target_ref", [])
                               if str(target.get("ref", "")).startswith("enemy:")]
                    x_schema = schema.get("x", {})
                    # 指令道纹的x是固定整数；玩家道纹的x是{minimum,maximum}范围
                    if isinstance(x_schema, dict):
                        x = max(1, min(5, x_schema["maximum"]))
                    else:
                        x = int(x_schema)
                    params = {"daowen_name": schema["daowen_name"], "x": x,
                              "dodge": False, "blood_shadow": False,
                              "trigger_spell_choices": {}}
                    if "actor_ref" in schema: params["actor_ref"] = schema["actor_ref"]
                    if schema["daowen_name"] == "波及" and engine is not None:
                        # 波及X：必须恰好提交X个存活非自身目标（schema上限已按目标数封顶）
                        refs = engine.combat._combat_entity_refs()
                        actor = refs.get(schema.get("actor_ref", "")) or engine.state.player
                        candidates = [r for r, e in refs.items()
                                      if e.is_alive and e is not actor]
                        params["dodge_targets"] = [{"target_ref": r, "dodge": False,
                                                    "blood_shadow": False}
                                                   for r in candidates[:x]]
                    elif enemies:
                        params["target_ref"] = enemies[0]["ref"]
                    return AIDecision("use_daowen", params, "使用合法道纹选项")
                if action_type == "prepare_attack":
                    actor_schema = action["params_schema"]["actor_ref"]
                    actor_ref = actor_schema[0]["ref"] if isinstance(actor_schema, list) else actor_schema
                    return AIDecision("prepare_attack", {"actor_ref": actor_ref}, "准备攻击")

        return AIDecision("noop", {}, "无可用行动")


# ========== 便捷工厂函数 ==========

def create_ai_backend(provider: str = "placeholder", **kwargs) -> AIBackend:
    """
    创建AI后端的便捷函数
    
    用法：
        backend = create_ai_backend("gemini")          # 从环境变量读取key
        backend = create_ai_backend("groq", api_key="gsk_xxx")
        backend = create_ai_backend("openrouter")       # 免费模型
        backend = create_ai_backend("deepseek")
        backend = create_ai_backend("placeholder")      # 测试用
    """
    providers = {
        "gemini": GeminiBackend,
        "google": GeminiBackend,
        "groq": GroqBackend,
        "openrouter": OpenRouterBackend,
        "deepseek": DeepSeekBackend,
        "placeholder": PlaceholderBackend,
        "test": PlaceholderBackend,
    }
    
    provider = provider.lower()
    if provider not in providers:
        raise ValueError(f"未知AI提供商: {provider}。可用: {list(providers.keys())}")
    
    return providers[provider](**kwargs)


# ========== 统一 AI 玩家控制器 ==========

class AIPlayer:
    """第四宇宙的统一 AI 入口 —— **正式决策路径全部由 LLM 后端完成**。

    2026-09-29 用户令：不用规则型 AI，全部决策交给 LLM。
    2026-10-02 架构收口：本类**不再继承** ``TacticalAI``，也不再有规则战术
    兜底——旧的 ``tactical_combat=True`` 分支已删除。规则 AI（``engine/ai_tactics.py``）
    保留为隔离的实验/模拟工具，只能由 sim、probes 与测试直接实例化，永远不参与
    正式决策。

    决策契约：
    * 开局、选区、事件、局外行动、轮回者自己的战斗行动、他人回合的闪避/招架/
      反应法术，全部由 ``backend``（LLM）根据 ``get_state()`` 与
      ``available_actions`` 决策；**每个动作槽位提交一条决策**，槽位数由引擎的
      当前出手预算决定（基础 2 次，受【疯狂】/【无力】/【蓄锐·增】修正），
      不写死「每回合固定 2 次」；
    * LLM 只决定「提交什么」，引擎决定「如何执行」：If/Loop 由执行器按结算时的
      真实状态逐步求值，LLM 不展开循环、不预演引擎；
    * 一切动作仍统一经过 ``GameEngine.execute_action`` 与规则校验器：非法提交
      被引擎拒绝并作为结果返回，**不会**改由规则 AI 兜底；
    * LLM 返回非法动作或不可解析内容时，结果就是失败/中断，绝不静默切换策略；
    * 没有配置任何 LLM API key 时用 ``PlaceholderBackend``——它是离线测试桩，
      按写死的顺序提交合法动作，不是游戏策略，也不使用 TacticalAI。
    """

    def __init__(
        self,
        game_engine: GameEngine,
        backend: AIBackend = None,
        validator: RuleValidator = None,
        rule_sync: RuleSync = None,
        auto_validate: bool = True,
        max_retries: int = 3,
        verbose: bool = False,
    ):
        self.engine = game_engine
        self.verbose = verbose
        self.backend = backend or PlaceholderBackend()
        # 让后端能读到引擎实时状态，用于可选法器/遗物的显式决策
        # （可以不用，但不能不让用）。
        if not getattr(self.backend, "engine", None):
            try:
                self.backend.engine = game_engine
            except Exception:
                pass
        self.validator = validator or RuleValidator()
        self.rule_sync = rule_sync
        self.auto_validate = auto_validate
        self.max_retries = max_retries

        self._decision_history: list[dict] = []
        self._violation_callbacks: list[Callable] = []
        # 若玩家已在 AI 创建前完成属性分配，身份记忆立即生成；否则在 setup
        # 成功后的第一次统一决策前惰性生成。
        self._ensure_player_memory()

    @property
    def player(self):
        return self.engine.state.player

    def on_violation(self, callback: Callable):
        """注册违规回调"""
        self._violation_callbacks.append(callback)

    # ---------- 长期记忆：统一 AI 的身份、经历与性格反馈 ----------

    def _memory_seed(self) -> int | str:
        seed = getattr(self.engine.dice, "_seed", None)
        return seed if seed is not None else self.player.runtime_id

    def _ensure_player_memory(self) -> dict | None:
        player = self.engine.state.player
        if player is None or not player.is_alive:
            return None
        return ensure_memory(player, self._memory_seed())

    def _memory_snapshot(self) -> dict:
        """只采集可由引擎状态验证的事实，绝不把 AI 的叙事当成数值事实。"""
        player = self.engine.state.player
        if player is None:
            return {}
        enemies = [e for e in self.engine.state.enemies if e.is_alive]
        return {
            "hp": player.current_hp,
            "blood_limit": player.blood_limit,
            "mana": player.current_mana,
            "mana_limit": player.mana_limit,
            "speed": player.current_speed,
            "speed_limit": player.speed_limit,
            "shield": player.shield,
            "enemy_hp": sum(e.current_hp for e in enemies),
            "threat": sum(e.effective_attack_count() * e.effective_attack_power()
                           for e in enemies),
        }

    def _remember_success(self, before: dict, result: dict,
                          action_info: dict | None = None) -> None:
        memory = self._ensure_player_memory()
        player = self.engine.state.player
        if memory is None or player is None or not result.get("success"):
            return
        after = self._memory_snapshot()
        _facts, evidence = remember_action(
            memory, before, after, result, action_info,
            battle=self.engine.state.current_battle,
            round_no=self.engine.state.current_round,
        )
        for dimension, direction, reason in evidence:
            # 性格系统负责 EMA、置信度、方向描述；记忆模块只负责提供可解释证据。
            self.engine.update_personality(
                player, dimension, direction, evidence=reason,
            )

    def _attach_memory_context(self, state: dict) -> dict:
        player = self.engine.state.player
        if player is not None and player.is_alive:
            memory = self._ensure_player_memory()
            if memory is not None:
                # _build_user_prompt 会把 state.state 序列化给后端；摘要单独放入，
                # 让模型看到相关记忆而不必依赖完整 episode 原文。
                state.setdefault("state", {})["ai_memory_context"] = context_for_ai(memory)
        return state

    def _finish_decision(self, decision: AIDecision, result: dict) -> dict:
        """统一执行校验、违规回调、规则同步和历史记录。"""
        validation = {"valid": True, "violations": [], "warnings": []}
        if self.auto_validate:
            validation = self.validator.validate(self.engine.state, {
                "action": decision.action_type,
                "params": decision.params
            }, result)
            if not validation["valid"]:
                for callback in self._violation_callbacks:
                    try:
                        callback(validation)
                    except Exception:
                        pass

        sync_report = None
        if self.rule_sync:
            changes = self.rule_sync.check_for_changes()
            if changes:
                sync_report = self.rule_sync.generate_sync_report()

        record = {
            "decision": decision.to_dict(),
            "result": result,
            "validation": validation,
            "sync_report": sync_report,
            "timestamp": time.time(),
        }
        self._decision_history.append(record)
        return {
            "action": decision.action_type,
            "params": decision.params,
            "reasoning": decision.reasoning,
            "result": result,
            "validation": validation,
            "sync_report": sync_report,
            "interrupt": result.get("interrupt"),
        }

    def play_turn(self, context: str = "") -> dict:
        """执行一个统一 AI 决策（每个动作槽位一条）。

        所有阶段（含轮回者战斗行动、他人回合的闪避/招架/反应法术）都由后端
        （LLM）决策。没有规则 AI 分支，也没有兜底：
        `get_state()` 生成状态与 available_actions，LLM 提交一个动作，
        引擎负责校验、结算与执行期语义（If/Loop、法力不足中断等）。
        """
        # 记忆与是否序列化状态无关：先保持既有副作用（命零前建立当轮记忆），
        # 待裁定判断改为直接读引擎字段——与 get_state()["pending_interrupts"] 同源。
        player = self.engine.state.player
        if player is not None and player.is_alive:
            self._ensure_player_memory()
        if self.engine._pending_interrupts:
            return {
                "action": "等待DM裁定",
                "interrupts": [i.to_dict() for i in self.engine._pending_interrupts],
                "instruction": "有中断等待DM裁定，AI无法继续决策",
            }
        state = self._attach_memory_context(self.engine.get_state())
        # get_state() 已经把 available_actions 生成好了（纯读，无副作用），
        # 不再重复生成一遍。
        available_actions = state.get("available_actions")
        if available_actions is None:
            available_actions = self.engine.get_available_actions()
        decision = self.backend.decide(state, available_actions, context)
        before = self._memory_snapshot()
        result = self.engine.execute_action(decision.action_type, decision.params)
        # setup_attributes 之前还没有轮回者，不生成一条虚假的“经历”；
        # 后续选区、事件和局外行动则进入同一份当前轮回记忆。
        if before:
            self._remember_success(before, result, {
                "action": decision.action_type,
                "params": decision.params,
                "label": decision.reasoning or decision.action_type,
            })
        else:
            self._ensure_player_memory()
        return self._finish_decision(decision, result)

    def get_history(self) -> list[dict]:
        return self._decision_history
    
    def get_stats(self) -> dict:
        total = len(self._decision_history)
        violations = sum(1 for d in self._decision_history 
                        if not d.get("validation", {}).get("valid", True))
        interrupts = sum(1 for d in self._decision_history 
                        if d.get("result", {}).get("interrupt"))
        
        return {
            "total_decisions": total,
            "violations_found": violations,
            "interrupts_triggered": interrupts,
            "compliance_rate": f"{(total - violations) / total * 100:.1f}%" if total > 0 else "N/A"
        }


class AIWithRetry(AIPlayer):
    """带重试的AI玩家"""
    
    def play_turn(self, context: str = "") -> dict:
        for attempt in range(self.max_retries):
            result = super().play_turn(context)
            
            if result.get("validation", {}).get("valid", True):
                return result
            
            violations = result["validation"].get("violations", [])
            violation_desc = "; ".join(
                v.get("violation_description", "") for v in violations
            )
            context = f"{context}\n注意：上次决策违规了：{violation_desc}。请重新决策。"
        
        result["retry_exhausted"] = True
        return result
