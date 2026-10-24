# webapp · 可玩网页界面

把引擎的开放世界接口包成 HTTP + 一张单页地图。界面只负责显示与转发，
所有判定仍由 `GameEngine.execute_action` 完成。

## 启动

```bash
python3 webapp/server.py       # 0.0.0.0:8000
PORT=9000 python3 webapp/server.py
```

只依赖 Python 标准库，不需要 `pip install`。

## 能做什么

- **移动/等待**：点相邻格或 WASD／方向键；每步推进世界时间，云雾按玩家视野揭开。
- **交涉**：相邻格的朋友、员工、商人、其他轮回者可交谈/交易/合作/威吓/动手。
- **建议同伴**：朋友与员工在地图上自主行动（默认跟随）。玩家只能建议
  `follow` / `guard` / `explore` / `hold`，是否照做由该单位按
  `engine/world.py::suggestion_acceptance_percent` 判定，拒绝时行为保持不变。
- **战斗**：地图遭遇后进入真实战斗引擎——准备攻击时逐击分配伤害，
  敌方阶段可对指定敌人尝试闪避，同伴可用 `resolve_ally_phases` 自主出手；
  救赎、回合结算、战终结算均有对应按钮。
- **事件**：地点事件按引擎给出的选项结算；需要额外参数的选项可在
  「附加参数 JSON」里补，例如 `{"resonance_type":"转换"}`。
- **高级指令**：侧栏可直接提交任意引擎 action（施法、道纹、消耗品等）。

## 接口

| 方法与路径 | 作用 |
|---|---|
| `GET /` | 单页界面 |
| `GET /api/snapshot` | 当前快照（玩家、迷雾地图、同伴、战斗、事件、日志） |
| `POST /api/new` | 开局：属性/副本/地图尺寸、可选演示同伴 |
| `POST /api/action` | `{action, params}` 直接调用引擎 |
| `POST /api/ui_attack` | 用当前 `prepare_attack` 快照提交逐击伤害 |
| `POST /api/ui_monster_phase` | 生成合法怪物阶段提交并结算（可带 `dodge_refs`） |

## 口径

- 单进程单局：`POST /api/new` 会替换当前对局。
- 快照只下发 `world_status`/`world_vision` 已公开的信息，不把全地图交给浏览器。
- 事件待结算时地图冻结，沿用上一帧视图（`world.stale=true`）。
- 开局默认自动选第一件遗物、第一枚道纹，共鸣取「转换」；这是界面层的省事默认，
  不改变引擎规则。
