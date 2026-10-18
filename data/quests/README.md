# 开放世界任务定义

`data/quests/quests.toml` 是任务内容事实源。任务引擎负责状态、进度、失败、奖励和任务链，不自动生成未经确认的剧情任务。

## 生命周期

```text
available -> active -> completed -> claimed
                    -> failed
```

- `available`：所有前置任务已完成/领取，且任务尚未接取。
- `active`：已接取，目标只能由引擎事实推进。
- `completed`：所有目标达成，可领取奖励。
- `claimed`：奖励已领取，不能重复领取。
- `failed`：命中失败事件或超过时限。

## 定义字段

每个 `[[quests]]` 至少包含：

- `id`、`title`、`objectives`、`rewards`。
- `description`：任务说明。
- `giver_name` 或 `giver_actor_id`：NPC委托；接取时必须与对应单位上下左右相邻。
- `prerequisites`：前置任务ID数组。
- `next_quests`：完成时写入解锁故事标记的后续任务ID数组。
- `deadline_turns`：可选时限，以世界回合计算。
- `fail_on`：可选失败事件名，或包含 `event` / `match` 的事件匹配表。

每个目标包含 `id`、`event`、可选 `match`、`required`。`match` 使用点路径读取事实字段；值 `"$player"` 表示当前轮回者。例如：

```toml
[[quests]]
id = "example_not_canonical"
title = "示例模板（不可作为正式剧情任务）"
description = "仅展示结构。正式任务内容须经确认后再写入。"
prerequisites = []
next_quests = []
rewards = { shards = 5, attribute_points = 0 }

[[quests.objectives]]
id = "reach_cell"
event = "actor_moved"
required = 1
match = { actor_id = "$player", "details.destination" = [2, 1] }
```

## 当前边界

- 任务引擎与API已经建立，但任务源当前没有正式任务条目。
- 已接入世界移动、等待及成功引擎行动事实；NPC交互、战斗胜负、物品获取等专用事实仍需逐一接入对应真实结算点。
- 当前可配置奖励为碎片与属性点；不支持任意脚本奖励。
- 故事内容由任务定义提供；引擎只推进事实与状态，不代写角色台词或故事正典。
