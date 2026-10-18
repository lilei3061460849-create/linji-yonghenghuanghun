"""结构化事实源的加载器（致死类特殊事件 + 核心规则压缩批）。

## 方向

旧机制 `engine/rule_sync.py` 是**从 markdown 正则抓取、再和引擎注册表比对**——
它只能数出「文档提到 N 条、引擎注册 N 条」，真冲突时谁对并无断言，是软护栏。

新机制反过来：**`data/rules/*.toml` 是源**，引擎常量、进度渲染、死者之书文案、
《死者之书.md》的「## 规则」节全部由它派生。这才有唯一事实源。

## 三份事实源的分工

- `lethal_events`（致死类特殊事件）：结构化数值**直接驱动引擎**（阈值/死因/进度串）。
- `special_events`（非致死类特殊事件）：同样**直接驱动引擎**（雕塑的伤害/格挡/耐久比），
  与 `lethal_events` 同署名同章节「特殊事件」，共同渲染进《死者之书》「## 规则」节，
  order 合并排序、不许撞车（测试钉住）。schema 按非致死事件来：没有死因文案/进度
  计数，数值字段随事件自身需要。
- `game_rules`（核心规则压缩批）：不驱动引擎结算（引擎行为以代码与《规则正文》
  为准），管的是「规则讲述」本身——`rule_lines` 是压缩后的玩家版本（进《死者
  之书》「## 规则」节），`precise_lines` 是规则正文原文照录（一致性测试断言与
  规则正文逐字一致，压缩不得悄悄漂移）。

## 引擎怎么读

`Entity.MUTATION_COLLAPSE_THRESHOLD` 这类**类属性在 import 时从本模块取值**，
所以全仓库那几十处 `Entity.MUTATION_COLLAPSE_THRESHOLD` 的写法一个都不用改，
但事实源已经变成了 toml。这是低风险的接线方式：改规则只动 toml。

## 格式选择

用 TOML 而非 YAML：引擎至今零第三方依赖（纯 stdlib），运行环境是 PEP-668
受管的，为一个事实源引入 PyYAML 不值得；`tomllib` 是 3.11 自带，且支持注释。
若日后愿意接受依赖，只需改本文件的 `_load`。
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "data" / "rules"

LETHAL_EVENTS_FILE = RULES_DIR / "lethal_events.toml"
SPECIAL_EVENTS_FILE = RULES_DIR / "special_events.toml"
GAME_RULES_FILE = RULES_DIR / "game_rules.toml"


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


class _LethalEvents:
    """致死类特殊事件的事实源访问器（模块级单例 `lethal_events`）。"""

    def __init__(self, path: Path = LETHAL_EVENTS_FILE) -> None:
        self._path = path
        self._doc = _load(path)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, dict[str, Any]] = {}
        for entry in self._doc.get("event", []):
            self._by_id[entry["id"]] = entry
            self._by_name[entry["name"]] = entry

    # ---- 元信息 ----
    @property
    def signature(self) -> str:
        """《死者之书》规则节的署名（遗言署人名、规则署 ？？？）。"""
        return self._doc.get("signature", "？？？")

    @property
    def chapter(self) -> str:
        return self._doc.get("chapter", "特殊事件")

    # ---- 查询 ----
    def all(self) -> list[dict[str, Any]]:
        return sorted(self._doc.get("event", []), key=lambda e: e.get("order", 0))

    def by_id(self, event_id: str) -> dict[str, Any]:
        try:
            return self._by_id[event_id]
        except KeyError:
            raise KeyError(f"致死事件 id 不存在: {event_id}") from None

    def by_name(self, name: str) -> dict[str, Any]:
        """按名称查条目。显示键带 `·` 后缀时按前缀查（`凡庸·未出手` → 凡庸）。"""
        if name in self._by_name:
            return self._by_name[name]
        head = name.split("·", 1)[0]
        if head in self._by_name:
            return self._by_name[head]
        raise KeyError(f"致死事件名称不存在: {name}")

    def progress_keys(self, event_id: str) -> Optional[list[str]]:
        """该事件在进度面板上的显示键（多数事件就等于事件名）。"""
        return self.by_id(event_id).get("progress_keys")

    # ---- 引擎消费的字段 ----
    def applies_to(self, event_id: str, entity_type: str) -> bool:
        """该角色类型是否会触发事件；未声明类型豁免时对所有角色生效。"""
        return entity_type not in self.by_id(event_id).get("excluded_entity_types", ())

    def threshold(self, event_id: str) -> int:
        """读取规则源中的阈值常量；还债将其作为动态公式的基数（10+历史最高碎片余额）。"""
        entry = self.by_id(event_id)
        if "threshold" not in entry:
            raise KeyError(f"致死事件 {event_id} 没有固定阈值，请用 threshold_multiplier")
        return int(entry["threshold"])

    def threshold_multiplier(self, event_id: str) -> float:
        entry = self.by_id(event_id)
        if "threshold_multiplier" not in entry:
            raise KeyError(f"致死事件 {event_id} 没有倍率阈值，请用 threshold")
        return float(entry["threshold_multiplier"])

    def progress_format(self, event_id: str) -> str:
        return self.by_id(event_id)["progress_format"]

    def death_cause_key(self, event_id: str) -> str:
        return self.by_id(event_id)["death_cause_key"]

    def death_cause_text(self, event_id: str) -> str:
        return self.by_id(event_id)["death_cause_text"]

    def rule_lines(self, event_id: str) -> list[str]:
        return list(self.by_id(event_id)["rule_lines"])

    def counter_field(self, event_id: str) -> str:
        return self.by_id(event_id)["counter_field"]

    def alt_counter_field(self, event_id: str) -> Optional[str]:
        return self.by_id(event_id).get("alt_counter_field")


lethal_events = _LethalEvents()


class _SpecialEvents:
    """非致死类特殊事件的事实源访问器（模块级单例 `special_events`）。

    与 `lethal_events` 并列：同署名、同章节「特殊事件」，共同渲染进《死者之书》
    「## 规则」节（生成器按 order 合并排序）。schema 按非致死事件来——没有死因
    文案/进度计数，数值字段随事件自身需要（雕塑：耐久比与每耐久伤害/格挡）。
    """

    def __init__(self, path: Path = SPECIAL_EVENTS_FILE) -> None:
        self._path = path
        self._doc = _load(path)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, dict[str, Any]] = {}
        for entry in self._doc.get("event", []):
            self._by_id[entry["id"]] = entry
            self._by_name[entry["name"]] = entry

    # ---- 元信息 ----
    @property
    def signature(self) -> str:
        """《死者之书》规则节的署名（遗言署人名、规则署 ？？？）。"""
        return self._doc.get("signature", "？？？")

    @property
    def chapter(self) -> str:
        return self._doc.get("chapter", "特殊事件")

    # ---- 查询 ----
    def all(self) -> list[dict[str, Any]]:
        return sorted(self._doc.get("event", []), key=lambda e: e.get("order", 0))

    def by_id(self, event_id: str) -> dict[str, Any]:
        try:
            return self._by_id[event_id]
        except KeyError:
            raise KeyError(f"非致死特殊事件 id 不存在: {event_id}") from None

    def by_name(self, name: str) -> dict[str, Any]:
        if name in self._by_name:
            return self._by_name[name]
        raise KeyError(f"非致死特殊事件名称不存在: {name}")

    # ---- 消费字段 ----
    def rule_lines(self, event_id: str) -> list[str]:
        return list(self.by_id(event_id)["rule_lines"])

    def threshold(self, event_id: str) -> int:
        """读取非致死事件中的通用固定阈值（如还债）。"""
        return int(self.by_id(event_id)["threshold"])

    # ---- 引擎消费的字段（雕塑）----
    def durability_ratio(self, event_id: str) -> float:
        """雕塑消耗品耐久上限 = ceil([血限] × 本系数)。"""
        return float(self.by_id(event_id)["durability_ratio"])

    def durability_min(self, event_id: str) -> int:
        """雕塑消耗品耐久下限（血限过低时保底）。"""
        return int(self.by_id(event_id)["durability_min"])

    def damage_per_durability(self, event_id: str) -> int:
        """雕塑：每消耗1点耐久，对1个[目标]造成的伤害。"""
        return int(self.by_id(event_id)["damage_per_durability"])

    def shield_per_durability(self, event_id: str) -> int:
        """雕塑：每消耗1点耐久，自身获得的格挡。"""
        return int(self.by_id(event_id)["shield_per_durability"])


special_events = _SpecialEvents()


class _GameRules:
    """核心规则（压缩批）的事实源访问器（模块级单例 `game_rules`）。

    不驱动引擎结算；管的是规则讲述本身：`rule_lines` 是压缩后的玩家版本
    （进《死者之书》「## 规则」节「核心规则」章），`precise_lines` 是《规则正文》
    原文照录（tests/test_game_rules_source.py 断言与规则正文逐字一致）。
    """

    def __init__(self, path: Path = GAME_RULES_FILE) -> None:
        self._path = path
        self._doc = _load(path)
        self._by_id: dict[str, dict[str, Any]] = {}
        for entry in self._doc.get("rule", []):
            self._by_id[entry["id"]] = entry

    # ---- 元信息 ----
    @property
    def signature(self) -> str:
        """《死者之书》规则节的署名（遗言署人名、规则署 ？？？）。"""
        return self._doc.get("signature", "？？？")

    @property
    def chapter(self) -> str:
        return self._doc.get("chapter", "核心规则")

    # ---- 查询 ----
    def all(self) -> list[dict[str, Any]]:
        return sorted(self._doc.get("rule", []), key=lambda e: e.get("order", 0))

    def by_id(self, rule_id: str) -> dict[str, Any]:
        try:
            return self._by_id[rule_id]
        except KeyError:
            raise KeyError(f"核心规则 id 不存在: {rule_id}") from None

    # ---- 消费字段 ----
    def rule_lines(self, rule_id: str) -> list[str]:
        """压缩讲述（进死者之书）。"""
        return list(self.by_id(rule_id)["rule_lines"])

    def precise_lines(self, rule_id: str) -> list[str]:
        """完整正文：《规则正文》原文照录（防漂移的事实锚点）。"""
        return list(self.by_id(rule_id)["precise_lines"])


game_rules = _GameRules()
