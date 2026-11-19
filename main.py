"""
linji-disiyuzhou 游戏引擎 - 主入口

使用方式：
    python main.py              # 启动交互式命令行
    python main.py --api        # API演示
    python main.py --validate   # 规则校验演示
    python main.py --sync       # 规则同步演示
    python main.py --full       # 完整流程演示（含校验+同步）
"""
import sys
import json
import os
import math

# 确保在项目根目录运行
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from engine.api import GameEngine
from engine.validator import RuleValidator, RuleViolation
from engine.rule_sync import RuleSync
from engine.ai_player import AIPlayer, PlaceholderBackend, AIWithRetry, create_ai_backend


# ========== 免费AI快速测试 ==========

def free_ai_test():
    """免费AI快速测试 - 自动检测可用的免费AI后端"""
    print("=" * 60)
    print("免费AI后端测试")
    print("=" * 60)
    
    backends = []
    
    # 检测可用的后端
    print("\n检测可用的免费AI后端...")
    
    if os.environ.get("GEMINI_API_KEY"):
        try:
            b = create_ai_backend("gemini")
            backends.append(("Gemini (Google免费)", b))
            print("  ✓ Gemini - 已配置")
        except Exception as e:
            print(f"  ✗ Gemini - {e}")
    else:
        print("  ○ Gemini - 未设置 GEMINI_API_KEY")
        print("    免费获取: https://aistudio.google.com/apikey")
    
    if os.environ.get("GROQ_API_KEY"):
        try:
            b = create_ai_backend("groq")
            backends.append(("Groq (免费超快)", b))
            print("  ✓ Groq - 已配置")
        except Exception as e:
            print(f"  ✗ Groq - {e}")
    else:
        print("  ○ Groq - 未设置 GROQ_API_KEY")
        print("    免费获取: https://console.groq.com/keys")
    
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            b = create_ai_backend("openrouter")
            backends.append(("OpenRouter (免费模型)", b))
            print("  ✓ OpenRouter - 已配置")
        except Exception as e:
            print(f"  ✗ OpenRouter - {e}")
    else:
        print("  ○ OpenRouter - 未设置 OPENROUTER_API_KEY")
        print("    免费获取: https://openrouter.ai/keys")
    
    if os.environ.get("DEEPSEEK_API_KEY"):
        try:
            b = create_ai_backend("deepseek")
            backends.append(("DeepSeek (注册送额度)", b))
            print("  ✓ DeepSeek - 已配置")
        except Exception as e:
            print(f"  ✗ DeepSeek - {e}")
    else:
        print("  ○ DeepSeek - 未设置 DEEPSEEK_API_KEY")
        print("    免费获取: https://platform.deepseek.com/api_keys")
    
    # 总有占位符可用
    backends.append(("占位符 (测试用)", PlaceholderBackend()))
    print("  ✓ 占位符 - 始终可用")
    
    if not backends:
        print("\n没有可用的AI后端。")
        return
    
    # 用第一个可用的后端测试
    print(f"\n使用 {backends[0][0]} 进行测试...")
    
    engine = GameEngine(db_path="data/ai_test_rulings.db")
    ai = AIPlayer(engine, backend=backends[0][1])
    
    print("\n[AI自动开局]")
    for i in range(5):
        result = ai.play_turn(f"第{i+1}步")
        print(f"  决策: {result['action']}")
        if result.get('reasoning'):
            print(f"  原因: {result['reasoning'][:80]}")
        print(f"  结果: {'成功' if result.get('result', {}).get('success') else '失败'}")
        
        if result.get("interrupt"):
            print(f"  ⚠ 中断: {result['interrupt']['interrupt_type']}")
            break
    
    stats = ai.get_stats()
    print(f"\n[统计] 决策{stats['total_decisions']}次, 违规{stats['violations_found']}次, 合规率{stats['compliance_rate']}")
    
    print("\n" + "=" * 60)
    print("测试完成。")
    print("=" * 60)


def print_state(engine: GameEngine):
    """打印当前状态摘要"""
    state = engine.get_state()
    phase = state["state"]["phase"]
    
    print("\n" + "=" * 60)
    print(f"【阶段】{phase}")
    
    player = state["state"].get("player")
    if player:
        print(f"\n【轮回者】{player['name']}")
        print(f"  生命: {player['current_hp']}/{player['blood_limit']}")
        print(f"  法力: {player['current_mana']}/{player['mana_limit']}")
        print(f"  速度: {player['current_speed']}/{player['speed_limit']}")
        print(f"  格挡: {player['shield']}")
        print(f"  道纹: {list(player.get('dao_wen', {}).keys())}")
    
    print(f"\n碎片: {state['state']['shards']}")
    
    enemies = state["state"].get("enemies", [])
    if enemies:
        print(f"\n【敌方】")
        for e in enemies:
            print(f"  {e['name']}: {e['current_hp']}/{e['blood_limit']} HP")
    
    interrupts = state.get("pending_interrupts", [])
    if interrupts:
        print(f"\n【待处理中断】")
        for i in interrupts:
            print(f"  ⚠ {i['interrupt_type']}: {i['description'][:80]}...")
    
    print("=" * 60)


def print_actions(actions: dict):
    """打印可用行动"""
    print(f"\n--- 可用行动 ({actions.get('phase', '?')}) ---")
    
    if "actions" in actions:
        for i, action in enumerate(actions["actions"], 1):
            if isinstance(action, dict):
                name = action.get("id", action.get("type", "?"))
                desc = action.get("description", "")
                available = action.get("available", True)
                status = "✓" if available else "✗"
                print(f"  {i}. [{status}] {name}: {desc}")
            else:
                print(f"  {i}. {action}")
    
    if "required_actions" in actions:
        print(f"\n  必须完成:")
        for a in actions["required_actions"]:
            print(f"    → {a}")


def api_demo():
    """API调用演示"""
    engine = GameEngine()
    
    print("=" * 60)
    print("API演示：完整游戏流程")
    print("=" * 60)
    
    # 1. 开局 - 分配属性
    # 速限/法限按2属性点一档计价，必须传入偶数点数；25点未花完的部分会存入属性点池。
    blood_points, speed_points, mana_points = 11, 8, 6
    print(f"\n[1] 分配属性点：{blood_points}血/{speed_points}速/{mana_points}法")
    result = engine.execute_action("setup_attributes", {
        "name": "贾凡",
        "blood_points": blood_points,
        "speed_points": speed_points,
        "mana_points": mana_points,
    })
    if not result.get("success"):
        print(f"  ✗ 开局失败：{result.get('error', '未知错误')}")
        return
    setup_result = result["result"]
    print(
        f"  → 血限:{setup_result['blood_limit']}, "
        f"速限:{setup_result['speed_limit']}, 法限:{setup_result['mana_limit']}"
    )
    print(f"  → 出手次数: {setup_result['action_count']}")
    print(f"  → 属性点池: {setup_result['attribute_points_banked']}")

    # 2. 开局遗物发现：必须先从引擎列出的候选中显式选择1件。
    relic_choices = setup_result["relic_choices"]
    relic_choice = relic_choices[0]
    print(f"\n[2] 开局遗物发现：候选 {relic_choices}；选择：{relic_choice}")
    result = engine.execute_action("choose_discovered_relic", {"relic_name": relic_choice})
    if not result.get("success"):
        print(f"  ✗ 遗物选择失败：{result.get('error', '未知错误')}")
        return

    # 3. 初始道纹发现：同样必须从本次候选中显式选择。
    daowen_choices = result["result"].get("daowen_choices", [])
    daowen_choice = daowen_choices[0]
    print(f"\n[3] 初始道纹发现：候选 {daowen_choices}；选择：{daowen_choice}")
    result = engine.execute_action(
        "setup_choose_initial_daowen", {"daowen_name": daowen_choice}
    )
    if not result.get("success"):
        print(f"  ✗ 初始道纹选择失败：{result.get('error', '未知错误')}")
        return

    # 4. 选择残韵
    print("\n[4] 选择初始残韵：反转")
    result = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    if not result.get("success"):
        print(f"  ✗ 残韵选择失败：{result.get('error', '未知错误')}")
        return

    # 5. 选择副本
    print("\n[5] 选择副本：扭曲都市")
    result = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    if not result.get("success"):
        print(f"  ✗ 副本选择失败：{result.get('error', '未知错误')}")
        return
    print(f"  → 已拥有遗物：{result['result']['relics_owned']}")

    # 6. 局外行动
    print("\n[6] 局外行动：修行")
    result = engine.execute_action("pre_battle_action", {"sub_action": "修行"})
    if not result.get("success"):
        print(f"  ✗ 局外行动失败：{result.get('error', '未知错误')}")
        return
    print(f"  → {json.dumps(result.get('result', {}), ensure_ascii=False)}")

    # 7. 查看状态
    print("\n[7] 当前状态：")
    print_state(engine)
    
    print("\n" + "=" * 60)
    print("API演示结束。")
    print("=" * 60)


def validate_demo():
    """规则校验演示"""
    print("=" * 60)
    print("规则校验演示")
    print("=" * 60)
    
    validator = RuleValidator(db_path="data/demo_violations.db")
    engine = GameEngine()
    
    # 正常开局：速限/法限按2属性点一档计价，开局后按遗物→道纹→残韵→副本推进。
    setup = engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6
    })
    if not setup.get("success"):
        print(f"  ✗ 开局失败：{setup.get('error', '未知错误')}")
        return
    relic_choices = setup["result"]["relic_choices"]
    relic_result = engine.execute_action("choose_discovered_relic", {
        "relic_name": relic_choices[0],
    })
    if not relic_result.get("success"):
        print(f"  ✗ 遗物选择失败：{relic_result.get('error', '未知错误')}")
        return
    daowen_choices = relic_result["result"].get("daowen_choices", [])
    daowen_result = engine.execute_action("setup_choose_initial_daowen", {
        "daowen_name": daowen_choices[0],
    })
    if not daowen_result.get("success"):
        print(f"  ✗ 初始道纹选择失败：{daowen_result.get('error', '未知错误')}")
        return
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    region_result = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    if not region_result.get("success"):
        print(f"  ✗ 副本选择失败：{region_result.get('error', '未知错误')}")
        return
    
    # 测试1：正常行动校验
    print("\n[测试1] 正常修行行动：")
    result = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
    validation = validator.validate(engine.state, {"action": "pre_battle_action"}, result)
    print(f"  校验结果：{'✓ 通过' if validation['valid'] else '✗ 失败'}")
    print(f"  警告数：{validation['warnings_count']}")
    
    # 测试2：添加怪物并测试困境检测
    print("\n[测试2] 添加残血怪物，测试困境提醒：")
    from engine.models import Entity
    monster = Entity(name="千手蜈蚣", entity_type="怪物", blood_limit=120, 
                     current_hp=10, attack_count=6, attack_power=8)
    engine.state.enemies.append(monster)
    
    validation = validator.validate(engine.state, {}, {})
    print(f"  校验结果：{'✓ 通过' if validation['valid'] else '✗ 失败'}")
    for w in validation.get('warnings', []):
        print(f"  ⚠ {w.get('rule_name', '?')}: {w.get('violation_description', '?')}")
    
    # 测试3：DM处理违规
    print("\n[测试3] DM处理违规记录：")
    pending = validator.get_pending_violations()
    print(f"  待处理违规数：{len(pending)}")
    
    if pending:
        # 转正为特例
        result = validator.resolve_violation(
            pending[0]["id"], 
            "legitimize", 
            "千手蜈蚣残血属于正常情况，不是困境"
        )
        print(f"  → {result.get('action', result.get('decision', '?'))}")
    
    # 统计
    stats = validator.get_violation_stats()
    print(f"\n[统计]")
    print(f"  总违规数：{stats['total']}")
    print(f"  已处理：{stats['resolved']}")
    print(f"  特例数：{stats['exceptions_count']}")
    
    print("\n" + "=" * 60)
    print("校验演示结束。")


def sync_demo():
    """规则同步演示"""
    print("=" * 60)
    print("规则同步演示")
    print("=" * 60)
    
    sync = RuleSync(
        db_path="data/demo_sync.db",
        rules_dir="."
    )
    
    # 生成同步报告
    print("\n[1] 生成同步报告：")
    report = sync.generate_sync_report()
    
    print(f"  跟踪文件：{report['files_tracked']}")
    print(f"  变更数：{len(report['changes_detected'])}")
    
    # 提取道纹
    print("\n[2] 从规则正文提取道纹定义：")
    daowen = sync.extract_daowen_from_file("AI_EXPERIENCE.md")
    print(f"  提取到 {len(daowen)} 个道纹")
    for d in daowen[:5]:
        print(f"    • {d['name']}: {d['description'][:40]}...")
    
    # 提取怪物
    print("\n[3] 从副本索引及已实现副本文档提取怪物定义：")
    monsters = sync.extract_monsters_from_dungeon_index("副本索引.md")
    print(f"  提取到 {len(monsters)} 个怪物")
    for m in monsters[:3]:
        print(f"    • {m['name']}: {m['attack_count']}×{m['attack_power']}/{m['blood_limit']}")
    
    # 差异检测
    print("\n[4] 道纹差异检测：")
    diff = sync.diff_project_daowen()
    print(f"  文件中存在：{len(diff['in_file_only'])}个（引擎未注册）")
    print(f"  引擎中存在：{len(diff['in_engine_only'])}个（文件未定义）")
    print(f"  双方同步：{len(diff['in_both'])}个")
    
    if diff['in_file_only']:
        print(f"  文件独有: {diff['in_file_only'][:5]}")
    
    # 修改建议
    print("\n[5] 修改建议：")
    suggestions = sync.generate_project_patch_suggestions()
    for s in suggestions[:3]:
        print(f"  • [{s['type']}] {s['suggestion'][:60]}...")
    
    print("\n" + "=" * 60)
    print("同步演示结束。")


def full_demo():
    """完整流程演示：引擎 + 校验 + 同步 + AI"""
    print("=" * 60)
    print("完整流程演示：引擎 + 校验 + 同步 + AI玩家")
    print("=" * 60)
    
    # 初始化所有组件
    engine = GameEngine(db_path="data/demo_rulings.db")
    validator = RuleValidator(db_path="data/demo_violations.db")
    sync = RuleSync(
        db_path="data/demo_sync.db",
        rules_dir="."
    )
    
    # 注册违规回调
    def on_violation(v):
        print(f"\n  🚨 发现违规！")
        for violation in v.get("violations", []):
            print(f"     [{violation.get('severity')}] {violation.get('rule_name')}: {violation.get('violation_description')}")
    
    # 创建AI玩家
    ai = AIWithRetry(
        game_engine=engine,
        backend=PlaceholderBackend(),
        validator=validator,
        rule_sync=sync,
        auto_validate=True,
        max_retries=2
    )
    ai.on_violation(on_violation)
    
    # 演示AI自动开局
    print("\n[AI自动开局]")
    result = ai.play_turn("开始游戏，分配属性点")
    print(f"  AI决策: {result['action']}")
    print(f"  参数: {result['params']}")
    print(f"  原因: {result['reasoning']}")
    print(f"  校验: {'✓' if result.get('validation', {}).get('valid', True) else '✗'}")
    
    print("\n  初始道纹已自动获得：杀伐")

    # AI选择残韵
    result = ai.play_turn("选择残韵")
    print(f"\n  AI决策: {result['action']} → {result.get('result', {}).get('result', {}).get('resonance_type', '?')}")
    
    # AI选择副本
    result = ai.play_turn("选择副本")
    print(f"\n  AI决策: {result['action']} → {result.get('result', {}).get('result', {}).get('region', '?')}")
    
    # AI局外行动
    print("\n[AI局外阶段]")
    for i in range(3):
        result = ai.play_turn(f"局外行动第{i+1}次，精力{engine.state.energy}")
        print(f"  第{i+1}行动: {result['action']}")
    
    # 统计
    print("\n[AI统计]")
    stats = ai.get_stats()
    print(f"  总决策数: {stats['total_decisions']}")
    print(f"  违规数: {stats['violations_found']}")
    print(f"  合规率: {stats['compliance_rate']}")
    
    # 同步报告
    print("\n[规则同步状态]")
    report = sync.generate_sync_report()
    print(f"  变更: {len(report['changes_detected'])}个")
    print(f"  道纹同步: {report['daowen_diffs'].get('AI_EXPERIENCE.md', {}).get('synced', 0)}个")
    
    print("\n" + "=" * 60)
    print("完整演示结束。")
    print("=" * 60)


def interactive_mode():
    """交互式命令行模式"""
    engine = GameEngine()
    validator = RuleValidator()
    
    print("""
╔══════════════════════════════════════════════════════════════╗
║              第四宇宙 · 临济·第四宇宙                        ║
║              AI约束型游戏引擎 v0.2                            ║
║                                                              ║
║  核心原则：AI是决策者，程序是事实源                           ║
║  规则校验：每次行动自动检查合规性                             ║
╚══════════════════════════════════════════════════════════════╝

命令：
  state       - 查看当前状态
  actions     - 查看可用行动
  do <行动>   - 执行行动
  validate    - 手动校验当前状态
  violations  - 查看违规记录
  resolve <id> <fix|legitimize|ignore> [备注] - 处理违规
  exceptions  - 查看特例列表
  sync        - 检查规则文件同步
  random <池名> <数字> - 提交随机数
  ruling <类型> <裁定文本> - 提交DM裁定
  save        - 保存游戏
  load        - 加载游戏
  history     - 查看行动历史
  rulings     - 查看DM裁定历史
  quit        - 退出
""")
    
    while True:
        try:
            cmd = input("\n> ").strip()
            
            if not cmd:
                continue
            
            if cmd == "quit":
                print("再见。")
                break
            
            elif cmd == "state":
                print_state(engine)
            
            elif cmd == "actions":
                actions = engine.get_available_actions()
                print_actions(actions)
            
            elif cmd.startswith("do "):
                parts = cmd[3:].strip().split(" ", 1)
                action_type = parts[0]
                params = json.loads(parts[1]) if len(parts) > 1 else {}
                
                result = engine.execute_action(action_type, params)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                
                if result.get("random_required"):
                    print(f"\n⚠ 需要随机数！范围：{result.get('pool_range', '?')}")
            
            elif cmd == "validate":
                result = validator.validate(engine.state, {}, {})
                print(f"校验：{'✓ 通过' if result['valid'] else '✗ 失败'}")
                print(f"违规: {result['violations_count']}, 警告: {result['warnings_count']}")
                for v in result.get('violations', []):
                    print(f"  ✗ {v.get('rule_name')}: {v.get('violation_description')}")
                for w in result.get('warnings', []):
                    print(f"  ⚠ {w.get('rule_name')}: {w.get('violation_description')}")
            
            elif cmd == "violations":
                pending = validator.get_pending_violations()
                if not pending:
                    print("没有待处理的违规。")
                else:
                    for v in pending:
                        print(f"  [{v['id']}] [{v['severity']}] {v['rule_name']}: {v['violation_description'][:60]}")
            
            elif cmd.startswith("resolve "):
                parts = cmd[8:].strip().split(" ", 2)
                if len(parts) < 2:
                    print("格式: resolve <id> <fix|legitimize|ignore> [备注]")
                    continue
                vid = int(parts[0])
                decision = parts[1]
                note = parts[2] if len(parts) > 2 else ""
                result = validator.resolve_violation(vid, decision, note)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            
            elif cmd == "exceptions":
                exceptions = validator.get_exceptions()
                if not exceptions:
                    print("没有注册的特例。")
                else:
                    for e in exceptions:
                        print(f"  [{e['rule_name']}] {e['exception_key']}: {e['description']}")
            
            elif cmd == "sync":
                sync = RuleSync(db_path="data/rule_sync.db")
                report = sync.generate_sync_report()
                print(f"变更: {len(report['changes_detected'])}个")
                diff = report['daowen_diffs'].get('AI_EXPERIENCE.md', {})
                print(f"道纹同步: {diff.get('synced', 0)}个")
                if diff.get('new'):
                    print(f"新增: {diff['new']}个")
                if diff.get('missing'):
                    print(f"缺失: {diff['missing']}个")
            
            elif cmd.startswith("ruling "):
                parts = cmd[7:].strip().split(" ", 1)
                if len(parts) < 2:
                    print("格式: ruling <类型> <裁定文本>")
                    continue
                result = engine.submit_ruling(parts[0], parts[1])
                print(json.dumps(result, ensure_ascii=False, indent=2))
            
            elif cmd == "save":
                result = engine.save_game()
                print(json.dumps(result, ensure_ascii=False, indent=2))
            
            elif cmd == "load":
                result = engine.load_game()
                print(json.dumps(result, ensure_ascii=False, indent=2))
            
            elif cmd == "history":
                history = engine.get_action_history()
                for h in history[-10:]:
                    print(f"  [{h.get('round', '?')}] {h['action']}")
            
            elif cmd == "rulings":
                rulings = engine.get_rulings_history()
                for r in rulings:
                    print(f"  [{r['interrupt_type']}] {r['ruling_text'][:60]}...")
            
            else:
                print(f"未知命令: {cmd}")
        
        except KeyboardInterrupt:
            print("\n\n使用 'quit' 退出。")
        except Exception as e:
            print(f"错误: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == "--api":
            api_demo()
        elif mode == "--validate":
            validate_demo()
        elif mode == "--sync":
            sync_demo()
        elif mode == "--full":
            full_demo()
        elif mode == "--free-ai":
            free_ai_test()
        elif mode == "--help":
            print("""
用法: python main.py [模式]

模式:
  (无参数)     交互式命令行
  --api        API演示
  --validate   规则校验演示
  --sync       规则同步演示
  --full       完整流程演示
  --free-ai    免费AI后端测试
  --help       显示帮助

免费AI设置（任选一个，全部免费，无需信用卡）:

  1. Google Gemini（推荐，最稳定）:
     export GEMINI_API_KEY="你的key"
     免费获取: https://aistudio.google.com/apikey

  2. Groq（最快）:
     export GROQ_API_KEY="你的key"
     免费获取: https://console.groq.com/keys

  3. OpenRouter（模型最多）:
     export OPENROUTER_API_KEY="你的key"
     免费获取: https://openrouter.ai/keys

  4. DeepSeek（中文最强）:
     export DEEPSEEK_API_KEY="你的key"
     免费获取: https://platform.deepseek.com/api_keys
""")
        else:
            print(f"未知模式: {mode}。使用 --help 查看帮助。")
    else:
        interactive_mode()
