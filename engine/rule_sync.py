"""规则自动同步系统。

事实源分工：规则正文.md 提供通用规则正文，死者之书.md 提供法术与遗言格式，
物品索引.md 提供物品，副本索引.md 与其链接文档提供副本内容；结构化特殊事件事实源
还定义了初拥之夜授予的专属道纹。README.md 只作入口与 AI/维护速览，不参与规则抽取
（2026-10-09 迁移）。
"""
from __future__ import annotations
import os
import re
import json
import hashlib
import time
import sqlite3
from typing import Optional
from pathlib import Path
from .daowen import DaoWenEngine
from .dm_rulings import DMRulingsDB
from .document_sources import (COMMON_RULES_FILE, DEATH_BOOK_FILE, DUNGEON_INDEX_FILE,
                               ITEM_INDEX_FILE)


class RuleFile:
    """规则文件跟踪"""
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.content = ""
        self.hash = ""
        self.last_modified = 0.0
        self._load()
    
    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r', encoding='utf-8') as f:
                self.content = f.read()
            self.hash = hashlib.md5(self.content.encode()).hexdigest()
            self.last_modified = os.path.getmtime(self.filepath)
    
    def reload(self) -> bool:
        """重新加载，返回是否有变更"""
        old_hash = self.hash
        self._load()
        return self.hash != old_hash
    
    def save(self):
        """保存内容到文件"""
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.write(self.content)
        self.hash = hashlib.md5(self.content.encode()).hexdigest()
        self.last_modified = time.time()


class RuleSync:
    """管理多份正文事实源与引擎之间的同步。"""

    # README 仅入口/速览，精确通用条文由 COMMON_RULES_FILE 提供（2026-10-09 迁移）。
    DEFAULT_RULE_FILES = [COMMON_RULES_FILE, DEATH_BOOK_FILE, ITEM_INDEX_FILE,
                          DUNGEON_INDEX_FILE]
    
    def __init__(
        self, 
        rule_files: list[str] = None,
        db_path: str = "data/rule_sync.db",
        rules_dir: str = "."
    ):
        self.rules_dir = rules_dir
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # 规则文件列表；未显式传入时跟踪全部正文事实源。
        self._rule_files: dict[str, RuleFile] = {}
        selected_files = self.DEFAULT_RULE_FILES if rule_files is None else rule_files
        for f in selected_files:
            self.add_rule_file(f)
        
        # 同步记录
        self._init_db()
        
        # 提取的规则缓存
        self._extracted_rules: dict = {}
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                change_type TEXT NOT NULL,
                section TEXT DEFAULT '',
                old_content TEXT DEFAULT '',
                new_content TEXT DEFAULT '',
                synced INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rule_patches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                section TEXT NOT NULL,
                patch_type TEXT NOT NULL,
                old_text TEXT DEFAULT '',
                new_text TEXT NOT NULL,
                reason TEXT DEFAULT '',
                applied INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    
    def add_rule_file(self, filepath: str):
        """添加规则文件"""
        full_path = os.path.join(self.rules_dir, filepath)
        self._rule_files[filepath] = RuleFile(full_path)
    
    # ========== 变更检测 ==========
    
    def check_for_changes(self) -> list[dict]:
        """检查所有规则文件是否有变更"""
        changes = []
        for name, rf in self._rule_files.items():
            if rf.reload():
                change = {
                    "file": name,
                    "old_hash": rf.hash,
                    "new_hash": rf.hash,
                    "detected_at": time.time()
                }
                changes.append(change)
                self._log_change(name, "file_modified", "", "", rf.content[:500])
        return changes
    
    def _log_change(self, file_path: str, change_type: str, section: str, old_content: str, new_content: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO sync_log (file_path, change_type, section, old_content, new_content, synced, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (file_path, change_type, section, old_content[:2000], new_content[:2000], time.time())
        )
        conn.commit()
        conn.close()
    
    # ========== 规则提取 ==========
    
    def extract_daowen_from_file(self, filepath: str) -> list[dict]:
        """从通用规则或单个副本文档中提取道纹定义。"""
        full_path = Path(filepath) if Path(filepath).is_absolute() else Path(self.rules_dir) / filepath
        if not full_path.exists():
            return []
        content = full_path.read_text(encoding="utf-8")

        # 限定到通用道纹正文，避免把“冷却X/生命X”等代价定义误识别成道纹。
        # README 自 2026-10-09 起只作入口；精确条文由规则正文.md 提供。
        if full_path.name == COMMON_RULES_FILE and "### 道纹体系\n" in content:
            content = content.split("### 道纹体系\n", 1)[1].split("### 特殊事件", 1)[0]
        elif "道纹定义：" in content:
            content = content.split("道纹定义：", 1)[1].split("专属行动", 1)[0]
        elif "道纹网络】" in content and "专属行动" in content:
            content = content.split("道纹网络】", 1)[1].split("专属行动", 1)[0]

        # 死者之书中的特殊事件页可能引用「原初X」等机制术语，不属于道纹定义。
        # 结构化事件页必须排除在道纹抽取范围之外。
        if full_path.name == "死者之书.md" and "### ？？？·特殊事件·" in content:
            content = content.split("### ？？？·特殊事件·", 1)[0]

        definitions: dict[str, dict] = {}
        lines = content.splitlines()
        for line_number, line in enumerate(lines, 1):
            # 标准格式：道纹X（可选说明）：效果；副本正文统一使用此格式。
            standard = re.match(
                r"^(?:\d+\.)?([\u4e00-\u9fff]{2})X(?:（[^）]*）)?[：:](.+)$", line)
            if standard:
                name, description = standard.groups()
                definitions.setdefault(name, {
                    "name": name,
                    "description": description.strip(),
                    "source": filepath,
                    "line": line_number,
                })

            # 规则正文的原始/转化道纹使用“名称X（消耗/代价与效果）”内联格式。
            for name, description in re.findall(
                    r"([\u4e00-\u9fff]{2})X（([^（）]+)）", line):
                if not any(label in description for label in ("消耗", "代价", "支付")):
                    continue
                definitions.setdefault(name, {
                    "name": name,
                    "description": description.strip(),
                    "source": filepath,
                    "line": line_number,
                })

        return list(definitions.values())

    def extract_events_from_file(self, filepath: str) -> list[dict]:
        """从规则文件中提取事件定义"""
        full_path = os.path.join(self.rules_dir, filepath)
        if not os.path.exists(full_path):
            return []
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        events = []
        seen = set()
        
        # 格式1："事件名"：描述（引号+冒号）
        # 格式2："事件名"\n（引号+换行，描述在下一行）
        # 格式3：事件名：描述（无引号，2-10个汉字+冒号）
        patterns = [
            r'["\u201c]([^"\u201d]{2,20})["\u201d]\s*[：：]',  # 带引号+冒号
            r'["\u201c]([^"\u201d]{2,20})["\u201d]\s*\n',     # 带引号+换行
            r'^([\u4e00-\u9fff·]{2,12})[：：](?!.*[：：].*[/×])',  # 无引号
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, content, re.MULTILINE):
                name = match.group(1).strip()
                # 排除非事件的名称
                skip_words = {"基础", "核心", "规则", "效果", "代价", "消耗", "持续", "目标",
                             "发现", "拒绝", "忘忧", "可分配法力", "可分配属性点", "攻击力", "攻击次数",
                             "且", "或", "共计", "转化道纹", "碎片", "获得50格挡", "造成30伤害",
                             "击杀怪物获得碎片", "遗物", "体外心脏", "羔羊之泪", "红头绳", 
                             "猩红尖牙", "黑金名片", "罪业金库", "教父左轮", "共心环", 
                             "负岳碑", "真龙之心", "绝息淤泥", "皮衣", "活性土壤",
                             "格挡", "声明", "自由规则", "随机数规则", "整数规则", "自由规则·自由控",
                             "积木规则", "循环规则", "中断规则", "法术阶级规则", "属性与状态规则",
                             "局外修行", "轮回者", "微光者", "怪物", "获取机制", "先手顺序",
                             "出怪", "战斗背景", "敌方面板", "我方面板", "死者之书前言",
                             "所需道纹", "触发条件", "生效流程", "唯一", "战斗无缝继续",
                             "逃跑方行阻截", "追击方破局拦截",
                             "死斗先手与流程", "区域背景", "环形闭环主轨", "道纹定义",
                             "扭曲都市专属行动", "闭环节点代数定义", "罪孽都市专属行动",
                             "罪孽都市专属机制", "龙心谷专属行动", "龙族遗物",
                             "未见场景裁定落库法则", "核心因果与联系",
                             "遗物·猩红果实", "遗物·苍白之花", "遗物",}
                if name in skip_words or name in seen:
                    continue
                if len(name) < 2:
                    continue
                
                # 获取后续描述
                start = match.end()
                next_section = re.search(r'\n["\u201c\u300c]|\n【|\n[\u4e00-\u9fff]{2,10}[：:]', content[start:start+500])
                end = start + (next_section.start() if next_section else min(300, len(content) - start))
                desc = content[start:end].strip()
                
                seen.add(name)
                events.append({
                    "name": name,
                    "description_preview": desc[:200],
                    "source": filepath,
                    "position": match.start()
                })
        
        return events
    
    def extract_monsters_from_file(self, filepath: str) -> list[dict]:
        """从规则文件中提取怪物定义"""
        full_path = os.path.join(self.rules_dir, filepath)
        if not os.path.exists(full_path):
            return []
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        monsters = []
        
        # 匹配怪物格式：名称（攻击次数×攻击力/血限，道纹1，道纹2）
        pattern = r'([\u4e00-\u9fff\w]+)[（(](\d+)[×x](\d+)/(\d+)(?:[，,]([^\n)）]*))?[）)]'
        for match in re.finditer(pattern, content):
            name = match.group(1)
            atk_count = int(match.group(2))
            atk_power = int(match.group(3))
            hp = int(match.group(4))
            daowen_str = match.group(5) or ""
            
            # 解析道纹
            daowen_parts = re.findall(r'([\u4e00-\u9fff]{2})(\d+)', daowen_str)
            daowen = {name: int(val) for name, val in daowen_parts}
            
            monsters.append({
                "name": name,
                "attack_count": atk_count,
                "attack_power": atk_power,
                "blood_limit": hp,
                "daowen": daowen,
                "daowen_raw": daowen_str.strip(),
                "source": filepath
            })
        
        return monsters
    
    def extract_relics_from_file(self, filepath: str) -> list[dict]:
        """从物品索引提取遗物；保留旧方法名供现有调用方使用。"""
        return [item for item in self.extract_items_from_file(filepath)
                if item["kind"] == "relic"]

    def extract_items_from_file(self, filepath: str = "物品索引.md") -> list[dict]:
        """从物品索引的 Markdown 标题结构提取遗物、消耗品与法器。

        重名条目或没有效果正文的条目属于非法配置，会立即拒绝。
        """
        full_path = Path(self.rules_dir) / filepath
        if not full_path.exists():
            return []
        lines = full_path.read_text(encoding="utf-8").splitlines()
        items: list[dict] = []
        current_section = ""
        group_headings = {"扭曲都市废墟设施工具库"}

        for index, line in enumerate(lines):
            section_match = re.match(r"^##\s+(.+)$", line)
            if section_match:
                current_section = section_match.group(1).strip()
                continue
            item_match = re.match(r"^(###|####)\s+(.+)$", line)
            if not item_match or not current_section:
                continue
            level = len(item_match.group(1))
            name = item_match.group(2).strip()
            if name in group_headings:
                continue

            body = []
            for following in lines[index + 1:]:
                heading = re.match(r"^(#{1,6})\s+", following)
                if heading and len(heading.group(1)) <= level:
                    break
                if following.strip():
                    body.append(following.strip())
            if not body:
                raise ValueError(f"物品条目缺少效果正文：{name}")

            if "消耗品" in current_section:
                kind = "consumable"
            elif "法器" in current_section:
                kind = "artifact"
            else:
                kind = "relic"
            items.append({
                "name": name,
                "kind": kind,
                "category": current_section,
                "effect": "\n".join(body),
                "source": filepath,
            })

        names = [item["name"] for item in items]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"物品索引包含重复条目：{duplicates}")
        return items

    def extract_spells_from_file(self, filepath: str = "死者之书.md") -> list[dict]:
        """从《死者之书》的“法术大全”章节提取法术。"""
        full_path = Path(self.rules_dir) / filepath
        if not full_path.exists():
            return []
        lines = full_path.read_text(encoding="utf-8").splitlines()
        in_spells = False
        spells: list[dict] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if line == "## 法术大全":
                in_spells = True
                index += 1
                continue
            if in_spells and line.startswith("## "):
                break
            if in_spells and line.startswith("### "):
                name = line[4:].strip()
                fields = {}
                index += 1
                while index < len(lines) and not lines[index].startswith("##"):
                    field = re.match(r"^(所需道纹|触发条件|生效流程)：(.+)$", lines[index])
                    if field:
                        fields[field.group(1)] = field.group(2).strip()
                    index += 1
                if "所需道纹" not in fields or "生效流程" not in fields:
                    raise ValueError(f"法术条目缺少所需道纹或生效流程：{name}")
                required = [value.strip() for value in re.split(r"[，,]", fields["所需道纹"])
                            if value.strip()]
                spells.append({
                    "name": name,
                    "required_daowen": required,
                    "trigger_condition": fields.get("触发条件", ""),
                    "effect_flow": fields["生效流程"],
                    "rank": len(set(required)),
                    "source": filepath,
                })
                continue
            index += 1

        names = [spell["name"] for spell in spells]
        if len(names) != len(set(names)):
            raise ValueError("死者之书包含重复法术名称")
        return spells

    def extract_monsters_from_dungeon_index(
            self, filepath: str = "副本索引.md") -> list[dict]:
        """从索引登记的已实现副本文档提取怪物，草案不会进入结果。"""
        from .monsters import parse_monster_pool
        full_path = Path(self.rules_dir) / filepath
        if not full_path.exists():
            return []
        pools = parse_monster_pool(full_path)
        return [monster for monsters in pools.values() for monster in monsters]

    def extract_dungeon_daowen(
            self, filepath: str = "副本索引.md", include_drafts: bool = True) -> list[dict]:
        """提取副本专属道纹，并保留所属副本和实现状态。"""
        from .dungeons import load_dungeon_manifest
        index_path = Path(self.rules_dir) / filepath
        result = []
        for entry in load_dungeon_manifest(index_path):
            if not include_drafts and entry.status != "已实现":
                continue
            for daowen in self.extract_daowen_from_file(str(entry.path.resolve())):
                result.append({**daowen, "dungeon": entry.name, "status": entry.status})
        return result

    def extract_event_daowen(self, filepath: str = "data/rules/special_events.toml") -> list[dict]:
        """提取结构化特殊事件中明示授予的初拥专属道纹。"""
        import tomllib
        full_path = Path(self.rules_dir) / filepath
        if not full_path.exists():
            return []
        with full_path.open("rb") as source:
            document = tomllib.load(source)
        result = []
        for event in document.get("event", []):
            if event.get("id") != "chu_yong_zhi_ye":
                continue
            for line in event.get("rule_lines", []):
                for name in re.findall(r"获得初拥专属道纹【([^】]+)】", line):
                    result.append({"name": name, "event": event.get("name", "初拥之夜"),
                                   "source": str(full_path)})
        return result

    def extract_project_rules(self) -> dict:
        """按裁定后的多事实源分工提取当前项目规则。"""
        from .dungeons import load_dungeon_manifest
        index_path = Path(self.rules_dir) / "副本索引.md"
        manifest = load_dungeon_manifest(index_path)
        return {
            "common_daowen": self.extract_daowen_from_file(COMMON_RULES_FILE),
            "dungeon_daowen": self.extract_dungeon_daowen(include_drafts=True),
            "event_daowen": self.extract_event_daowen(),
            "spells": self.extract_spells_from_file(DEATH_BOOK_FILE),
            "items": self.extract_items_from_file(ITEM_INDEX_FILE),
            "dungeons": [
                {"name": entry.name, "tier": entry.tier, "status": entry.status,
                 "path": str(entry.path)} for entry in manifest
            ],
            "monsters": self.extract_monsters_from_dungeon_index("副本索引.md"),
        }
    
    # ========== 差异检测 ==========
    
    def diff_daowen(self, filepath: str) -> dict:
        """比较文件中的道纹与引擎注册的道纹"""
        file_daowen = self.extract_daowen_from_file(filepath)
        registered = DaoWenEngine.list_all()
        
        file_names = {d["name"] for d in file_daowen}
        reg_names = set(registered)
        
        return {
            "in_file_only": list(file_names - reg_names),
            "in_engine_only": list(reg_names - file_names),
            "in_both": list(file_names & reg_names),
            "file_daowen": file_daowen,
        }

    def diff_project_daowen(self) -> dict:
        """比较引擎与当前已实现正文中的通用及副本专属道纹。"""
        file_daowen = self.extract_daowen_from_file(COMMON_RULES_FILE)
        file_daowen += self.extract_dungeon_daowen(include_drafts=False)
        # 初拥选项5中的专属道纹定义于结构化特殊事件事实源，而非通用/副本正文。
        file_daowen += self.extract_event_daowen()
        file_names = {item["name"] for item in file_daowen}
        registered = set(DaoWenEngine.list_all())
        # 已实现副本也可以登记“支付待定”的草案。草案必须显式进入
        # UNIMPLEMENTED_DAOWEN，单独报告，不能冒充运行时缺口或已注册道纹。
        from .gamedata import UNIMPLEMENTED_DAOWEN
        draft_names = set(UNIMPLEMENTED_DAOWEN)
        file_only = file_names - registered
        return {
            "in_file_only": sorted(file_only - draft_names),
            "draft_only": sorted(file_only & draft_names),
            "in_engine_only": sorted(registered - file_names),
            "in_both": sorted(file_names & registered),
            "file_daowen": file_daowen,
        }
    
    # ========== 自动修改建议 ==========
    
    def generate_patch_suggestions(self, filepath: str) -> list[dict]:
        """根据差异生成修改建议"""
        suggestions = []
        
        # 道纹差异
        daowen_diff = self.diff_daowen(filepath)
        for name in daowen_diff["in_file_only"]:
            suggestions.append({
                "type": "new_daowen",
                "name": name,
                "file": filepath,
                "suggestion": f"新道纹【{name}】在规则文件中发现但引擎未注册，需要添加计算函数",
                "auto_fixable": False,
                "action_required": "在 daowen.py 中添加 calculate_{name} 函数"
            })
        
        for name in daowen_diff["in_engine_only"]:
            suggestions.append({
                "type": "orphan_daowen",
                "name": name,
                "file": filepath,
                "suggestion": f"引擎中注册了道纹【{name}】但规则文件中未找到定义",
                "auto_fixable": True,
                "action_required": "确认是否应移除或补充规则文件"
            })
        
        return suggestions

    def generate_project_patch_suggestions(self) -> list[dict]:
        """根据全部已实现正文与引擎的差异生成建议。"""
        suggestions = []
        diff = self.diff_project_daowen()
        for name in diff["in_file_only"]:
            suggestions.append({
                "type": "new_daowen", "name": name, "file": "多事实源",
                "suggestion": f"已实现正文定义了道纹【{name}】但引擎未注册",
                "auto_fixable": False,
                "action_required": f"在 daowen.py 中添加 calculate_{name} 函数",
            })
        for name in diff["in_engine_only"]:
            suggestions.append({
                "type": "orphan_daowen", "name": name, "file": "多事实源",
                "suggestion": f"引擎注册了道纹【{name}】但已实现正文未定义",
                "auto_fixable": False,
                "action_required": "确认应删除实现或补充对应正文",
            })
        return suggestions
    
    def generate_sync_report(self) -> dict:
        """生成完整的同步报告"""
        report = {
            "timestamp": time.time(),
            "files_tracked": list(self._rule_files.keys()),
            "changes_detected": [],
            "daowen_diffs": {},
            "facts": {},
            "suggestions": [],
            "unresolved_violations": 0,
        }
        
        # 检查变更
        changes = self.check_for_changes()
        report["changes_detected"] = changes
        
        # 通用道纹只与规则正文（规则正文.md）比较；其他 Markdown 各自使用专用提取器，
        # 避免把物品标题或法术字段误报成道纹。
        if COMMON_RULES_FILE in self._rule_files and DUNGEON_INDEX_FILE in self._rule_files:
            diff = self.diff_project_daowen()
            report["daowen_diffs"][COMMON_RULES_FILE] = {
                "new": len(diff["in_file_only"]),
                "missing": len(diff["in_engine_only"]),
                "synced": len(diff["in_both"]),
                "details": diff,
                "scope": "规则正文通用道纹+已实现副本专属道纹",
            }
            report["suggestions"].extend(self.generate_project_patch_suggestions())

        required_sources = set(self.DEFAULT_RULE_FILES)
        if required_sources.issubset(self._rule_files):
            facts = self.extract_project_rules()
            report["facts"] = {name: len(values) for name, values in facts.items()}

        return report
    
    # ========== 自动修改 ==========
    
    def auto_apply_patch(
        self, 
        file_path: str, 
        section: str, 
        old_text: str, 
        new_text: str,
        reason: str = ""
    ) -> dict:
        """
        自动应用补丁到规则文件
        会先创建补丁记录，然后应用
        """
        # 保存补丁
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """INSERT INTO rule_patches (file_path, section, patch_type, old_text, new_text, reason, applied, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (file_path, section, "replace", old_text, new_text, reason, time.time())
        )
        patch_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # 应用补丁
        full_path = os.path.join(self.rules_dir, file_path)
        if not os.path.exists(full_path):
            return {"success": False, "error": f"文件不存在: {full_path}"}
        
        rf = self._rule_files.get(file_path)
        if not rf:
            rf = RuleFile(full_path)
            self._rule_files[file_path] = rf
        
        if old_text and old_text in rf.content:
            rf.content = rf.content.replace(old_text, new_text, 1)
            rf.save()
            
            # 标记已应用
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE rule_patches SET applied = 1 WHERE id = ?", (patch_id,))
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "patch_id": patch_id,
                "file": file_path,
                "change": f"已替换 '{old_text[:50]}...' → '{new_text[:50]}...'"
            }
        elif not old_text:
            # 追加模式
            rf.content += "\n" + new_text
            rf.save()
            
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE rule_patches SET applied = 1 WHERE id = ?", (patch_id,))
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "patch_id": patch_id,
                "file": file_path,
                "change": f"已追加内容"
            }
        else:
            return {
                "success": False,
                "error": f"未找到匹配文本: '{old_text[:100]}'",
                "patch_id": patch_id
            }
    
    def add_rule_section(
        self,
        file_path: str,
        section_header: str,
        content: str,
        after_section: str = None
    ) -> dict:
        """在规则文件中添加新章节"""
        full_path = os.path.join(self.rules_dir, file_path)
        if not os.path.exists(full_path):
            return {"success": False, "error": f"文件不存在: {full_path}"}
        
        rf = self._rule_files.get(file_path)
        if not rf:
            rf = RuleFile(full_path)
            self._rule_files[file_path] = rf
        
        new_section = f"\n\n{section_header}\n{content}"
        
        if after_section and after_section in rf.content:
            # 在指定章节后插入
            idx = rf.content.index(after_section) + len(after_section)
            # 找到该章节的结束位置
            next_section = re.search(r'\n【', rf.content[idx:])
            if next_section:
                insert_pos = idx + next_section.start()
                rf.content = rf.content[:insert_pos] + new_section + rf.content[insert_pos:]
            else:
                rf.content += new_section
        else:
            rf.content += new_section
        
        rf.save()
        
        return {"success": True, "file": file_path, "section": section_header}
    
    # ========== 同步历史 ==========
    
    def get_sync_history(self, limit: int = 50) -> list[dict]:
        """获取同步历史"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_pending_patches(self) -> list[dict]:
        """获取待应用的补丁"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM rule_patches WHERE applied = 0 ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_patch_history(self, limit: int = 50) -> list[dict]:
        """获取补丁历史"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM rule_patches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
