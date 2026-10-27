# AI 酒馆 v0.12.0 架构与扩展接口

## 依赖方向

```text
AstrBot 指令 / Web 路由
          ↓
      服务与引擎
          ↓
  领域仓库 / 世界契约 / 叙事
          ↓
      SQLite / 独立存档
```

指令和 Web 路由不得直接执行 SQL；世界包解析器不得发送群消息；叙事生成器不得自行切换行动者；平台适配层不得修改副本状态。跨模块写操作必须经过服务、权限、事务防重与审计。

## 目录职责

```text
main.py                       AstrBot 事件、指令入口与平台回执
tavern/bootstrap.py           运行时依赖装配
tavern/api/                   公共服务、事件钩子与注册式扩展
tavern/repositories/          按领域拆分的数据访问方法
tavern/database.py            SQLite 连接、当前 Schema 与仓库组合入口
tavern/engine.py              回合、模型调用、质量守卫和原子提交
tavern/prompts.py             分用途上下文编译、精简投影与修复提示
tavern/presentation.py        群聊展示与角色卡格式化
tavern/stat_generation.py     多预设属性结算、快照、全组合校验与迁移评估
tavern/world_contract.py      世界协议 v2/v3 运行契约
tavern/world_preflight.py     导入前结构化体检
tavern/narrative_quality.py   通用叙事质量检查
tavern/emergency.py           管理员精确急救
tavern/diagnostics.py         脱敏诊断
tavern/web_console.py         管理台 API 路由
tavern/backup_service.py      完整备份导出与自动备份清理（A15）
pages/console/core/           前端桥接、DOM 与状态
pages/console/                页面入口与统一设计系统
```

## 数据仓库

`TavernDatabase` 是组合入口，具体职责分布如下：

| 仓库 | 职责 |
|---|---|
| `worlds.py` | 世界包、常驻 NPC、世界归档 |
| `sessions.py` | 群副本、玩家、回合顺序与会话状态 |
| `story.py` | 事件、记忆、快照与回合提交 |
| `rules.py` | 冻结配置、规则状态、Token 与事务回执 |
| `characters.py` | 参与者、建卡、审核与角色卡版本 |
| `workflow.py` | 开演、选项、投票、暂离、返场与退场 |
| `timers.py` | 计时策略、持久计时器与到期动作 |
| `admin.py` | 权限、审计、清理、备份导入导出 |
| `current_state.py` | 为当前 Schema 幂等补齐派生运行行 |

仓库 mixin 仅供数据库组合入口使用，不是公共扩展接口。

## 上下文编译

叙事模型不直接接收数据库行或完整世界包。`prompts.py` 按任务编译三种上下文：故事裁定、权威检定后续写、A—D 选项。编译层会移除建卡预设、作者界面配置、开场选项、事件池、时间戳、绑定码和重复角色档案，并将 NPC 合并为一次运行投影。结构修复只接收无效输出与校验错误，不嵌套原始大提示。

模型可见的属性同时包含稳定 ID 与中文标签；存档以稳定 ID 为权威，展示以冻结世界快照的标签为权威。上下文裁剪不能修改世界状态、检定回执或事务边界。

## 多预设属性结算

`stat_generation.py` 是 `preset_stack` 的唯一权威算法。建卡、恢复、字段修改、角色卡修订、确认、审核与世界体检都从 `base_stats` 重新计算，不在当前属性上累加。角色卡持久化最终 `raw/modifiers` 和 `stat_generation_snapshot`；检定仍只读取已确认角色卡中的最终修正，不在每次投骰时复算世界预设。

协议 v3 世界包必须声明最低插件版本，并通过全部合法组合体检。协议 v2 继续支持旧 `none/manual/preset` 世界，不执行新模式的静默降级。

## 公共扩展接口

可信 Python 扩展从 `tavern.api` 使用：

```python
plugin.extensions.register_dice_system("percentile", resolver)
plugin.extensions.register_narrative_guard("continuity", guard)
plugin.hooks.subscribe("story_generated", on_story_generated)

session = await plugin.public_api.get_current_session(platform_id, group_id)
turn = await plugin.public_api.get_turn_context(session["id"])

from tavern.api import (
    calculate_preset_stack_stats,
    validate_stat_generation_config,
)
```

可注册类型：

- `dice_system`
- `check_resolver`
- `world_validator`
- `narrative_guard`
- `summary_provider`
- `admin_action`
- `element_resolver`（v0.12.0-A14 起：元素反应自定义解析，异常回退声明式表）

骰制提供器使用关键字参数 `check`、`check_type`、`actors` 与 `outcome_policy`，返回 `DiceResult` 或可等待的 `DiceResult`。世界包声明的 `resolution.dice_system` 必须能在开演前解析到已注册提供器；不存在时拒绝开演，不会回退到 D20。

可观察事件（完整目录见 `tavern/api/hooks.py` 的 `SUPPORTED_EVENTS`）：

- `session_created` / `character_approved` / `turn_started` / `option_selected`
- `check_completed` / `vote_completed` / `story_generated` / `session_finished`
- `dm_mode_enabled` / `dm_mode_disabled` / `dm_assigned` / `dm_directive_saved`
- `dm_beat_started` / `dm_beat_committed` / `dm_narrative_appended`
- `dm_handoff_started` / `dm_handoff_completed` / `dm_taken_over`
- `preset_dimension_selected` / `preset_selection_rejected`
- `character_presets_resolved` / `knowledge_boundary_resolved`
- `content_boundary_resolved` / `knowledge_access_denied`
- `content_boundary_blocked`

钩子只在权威状态提交后收到脱离 SQL 连接的字典副本。钩子异常被隔离，单个异步回调最多执行 2 秒，不会回滚或长期阻塞主流程。

## 扩展安全边界

- 世界包始终为纯 JSON，不能携带或执行 Python/JavaScript。
- 扩展不能取得原始数据库连接，也不能绕过权限和操作回执。
- 写入剧情、创建存档和读取回合必须调用 `TavernPublicAPI`。
- 同名扩展不可重复注册；扩展名称必须稳定，避免存档中的规则引用漂移。
- 运行中世界契约仍然冻结；改变属性、职业或数值模式时应新建副本。

## 版本边界

v0.9.x 使用 Schema 8 和独立数据库 `catalog_v090.sqlite3`。v0.9.3 同时接受世界协议 v2 与 v3；v3 专用于需要 `preset_stack` 等新契约的世界。Schema 1—7 与世界协议 v1 仍不受支持。
