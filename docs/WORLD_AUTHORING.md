# v0.9.3 世界包编写说明

世界包是可复用模板。新建副本时会复制世界版本、规则与时间规则快照；之后修改模板不会静默改变正在运行的团。

安装包自带可直接复制的通用模板：

- `templates/world-package.template.json`：当前世界协议、建卡向导与机器骰制的最小完整示例。
- `templates/world-package-preset-stack.template.json`：多个预设共同生成属性的可运行示例。
- `templates/npc-import.template.json`：可直接通过管理台导入的常驻 NPC 数据示例。
- `templates/template-manifest.json`：模板兼容的插件、世界协议、角色卡和 NPC 导入版本。
- `templates/README.md`：面向世界作者与 AI 修改工具的逐项说明和发布维护规则。

不要直接修改模板原件后覆盖发布包；应复制并重命名，再替换世界标识、稳定 ID、显示文案和实际设定。

## 顶层字段

| 字段 | 作用 |
|---|---|
| `slug` | 唯一标识；小写字母、数字、`_`、`-`，最多 64 字符 |
| `name` | 显示名称 |
| `description` | 题材、规模与玩法简介 |
| `system_prompt` | 稳定世界规律、能力边界与叙事语气 |
| `opening_scene` | 第一次开演时的开场 |
| `rules` | 人数、角色卡、检定、内容边界、NPC、事件与时间规则 |
| `initial_state` | 新副本的地点、时间、摘要、事实、物品与关系 |

## 规则骨架

```json
{
  "resolution": {
    "mode": "attribute",
    "dice_system": "d20",
    "difficulty_policy": {
      "safe": null,
      "controlled": 9,
      "dangerous": 13,
      "desperate": 17,
      "lethal": 21
    },
    "outcome_policy": {
      "natural_20_critical": true,
      "natural_1_critical": true,
      "critical_success_margin": 10,
      "cost_success_min_margin": -4,
      "failure_min_margin": -9
    }
  },
  "strict_choices": true,
  "check_density": "standard",
  "player_limits": {},
  "character_card": {},
  "dice_rules": {},
  "inspiration": {},
  "content_boundaries": {},
  "progress": {},
  "npc_policy": {},
  "context_budget": {},
  "time_rules": {},
  "opening_choices": [],
  "event_pool": [],
  "safe_exit_templates": [],
  "return_rules": {}
}
```

世界 JSON 是声明式数据，不能包含脚本或可执行表达式。

## 步骤驱动预设

预设属于角色卡结构化数据，不要把可选名单写进 `system_prompt` 或字段标题。插件只在建卡进入当前字段时解析和展示该字段的预设。

```json
{
  "character_card": {
    "version": 4,
    "preset_sets": {
      "origin_regions": [
        {
          "id": "northern_kingdom",
          "value": "北境王国",
          "label": "北境王国",
          "summary": "王国腹地、农庄与边境堡垒。"
        }
      ]
    },
    "fields": [
      {
        "key": "origin_region",
        "label": "选择出身地区",
        "type": "preset_select",
        "preset_source": "origin_regions",
        "page_size": 5,
        "required": true,
        "max_chars": 20
      }
    ]
  }
}
```

玩家可回复当前页序号、`id`、`value`、`label` 或别名。角色卡保存展示值，并在 `_preset_refs` 中保存稳定 ID 与当时效果快照。`page_size` 范围为 `1..10`。

条件字段可使用：

```json
{
  "key": "contract_source",
  "label": "选择契约来源",
  "type": "preset_select",
  "preset_source": "warlock_contracts",
  "visible_when": {"profession": ["术士"]},
  "required": true
}
```

字段还可声明：

- `clear_on_change`：当前字段修改后需要清理的后续字段 key。
- `must_differ_from`：当前选择不得与指定字段相同。
- `options_source`：兼容既有世界包的预设源名称；新包优先使用 `preset_source`。

导入体检会拒绝不存在的预设源、必填字段无有效选项、非法字段引用与条件循环依赖。

## 机器骰制与 DC 权限

世界包只声明骰制名称和确定性策略。随机数、加值、DC 映射及成功档位由插件执行，文字 AI 没有重投或修改权限。

- `dice_system` 必须对应运行时已注册骰制；找不到时直接拒绝裁定。
- `difficulty_policy` 把风险等级映射为 DC，选项或模型给出的任意 DC 不会覆盖它。
- `outcome_policy` 决定大成功、成功、代价成功、失败和大失败的边界。
- 必检选项必须在展示给玩家之前带有属性、风险、类型和已知后果。
- 骰点先锁定并公开，再调用文字模型续写；模型重试复用同一回执。

## 人数

```json
{
  "player_limits": {
    "recommended_min": 2,
    "recommended_max": 4,
    "minimum_start": 2,
    "maximum": 4
  }
}
```

`recommended_min/max` 只用于展示；`minimum_start` 是开演前置检查；`maximum` 是数据库强制席位上限，范围 `1..32`。

## 玩家角色卡

角色卡模板在管理台中拥有独立入口：

```text
酒馆控制台 → 世界与角色 → 对应世界卡片 → 角色卡模板
```

该入口支持导入 JSON、导出 JSON、结构校验、玩家建卡表单预览、恢复默认和确认保存。导入与编辑不会即时生效，只有通过校验并点击保存后才会更新世界模板。它不应与常驻 NPC 管理混为一体。

```json
{
  "character_card": {
    "version": 4,
    "auto_approve": false,
    "edit_requires_review": true,
    "fields": [
      {
        "key": "name",
        "label": "角色姓名",
        "required": true,
        "private": false,
        "max_chars": 12,
        "type": "text"
      },
      {
        "key": "code",
        "label": "副本代号",
        "required": true,
        "private": false,
        "max_chars": 12,
        "type": "text"
      },
      {
        "key": "belief",
        "label": "核心信念",
        "required": true,
        "private": false,
        "max_chars": 300,
        "type": "text"
      },
      {
        "key": "secret",
        "label": "私人秘密",
        "required": false,
        "private": true,
        "max_chars": 600,
        "type": "text"
      }
    ],
    "stats": {
      "mode": "manual",
      "budget": 10,
      "attributes": [
        {
          "key": "body",
          "label": "体魄",
          "minimum": 0,
          "maximum": 5,
          "default": 2
        },
        {
          "key": "agility",
          "label": "敏捷",
          "minimum": 0,
          "maximum": 5,
          "default": 2
        }
      ],
      "modifier_table": {
        "0": -3,
        "1": -2,
        "2": -1,
        "3": 0,
        "4": 1,
        "5": 2
      }
    }
  }
}
```

校验规则：

- `version` 必须是正整数。
- `fields` 的 `key` 不可为空或重复，并必须包含 `name` 与 `code`。
- 属性 `key` 不可重复。
- 属性默认值必须在最小值与最大值之间。
- 总预算必须介于全部属性最小值之和与最大值之和之间。
- 插件会为属性自动补充 `stat_<key>` 建卡步骤。

使用 `preset_stack` 时，角色卡模板版本应为 4，世界协议必须为 v3，并声明 `minimum_plugin_version: "0.9.3"`。在 `character_card` 下增加：

```json
{
  "stat_generation": {
    "mode": "preset_stack",
    "base_stats": {"body": 2, "agility": 2},
    "bonus_sources": ["origin_region", "profession"],
    "bonus_source_rules": {
      "origin_region": {"expected_bonus_total": 1},
      "profession": {"expected_bonus_total": 1}
    },
    "expected_total": 6,
    "min_per_stat": 2,
    "max_per_stat": 4,
    "allow_manual_edit": false
  }
}
```

同时将 `stats.mode` 设为 `preset_stack`，并为每个来源选项写入非空 `stat_bonus`。所有来源选完后插件自动显示最终属性和来源、保存快照并继续后续字段，不创建 `stat_<key>` 手动填写步骤。返回修改来源时从基础值重算。

建议字段包含背景、目标、信念、羁绊、专长、缺陷、弱点、知识边界、私人秘密和内容边界。

## 检定规则

```json
{
  "dice_rules": {
    "system": "d20",
    "advantage": "2d20_keep_high",
    "disadvantage": "2d20_keep_low",
    "stacking": false,
    "opposites_cancel": true,
    "outcome_bands": true,
    "visibility": "public"
  },
  "inspiration": {
    "initial": 1,
    "maximum": 3,
    "uses": ["advantage_before_roll", "reroll_full_pool"]
  }
}
```

`visibility` 可为：

- `public`：显示骰池、取值、加值、DC、来源和结果。
- `immersive`：隐藏具体 DC。
- `hidden`：群内只显示叙事，完整数据留在后台。

建议内部测试固定使用 `public`。

## 风险与难度

DC 只表示成功难度：

| DC | 难度 |
|---:|---|
| 5 | 极易 |
| 8 | 容易 |
| 10 | 普通 |
| 12 | 标准 |
| 15 | 困难 |
| 18 | 非常困难 |
| 20 | 极限 |
| 25 | 传奇 |

风险只表示失败后果：

- `safe`
- `controlled`
- `dangerous`
- `desperate`
- `lethal`

`lethal` 必须提前填写玩家可见后果。不要让同一个原因同时提高 DC 和造成劣势。

## 正式进度

```json
{
  "progress": {
    "chapter": "第一章：暴雪来客",
    "current_objective": "调查酒馆地下的异响",
    "completed_milestones": 2,
    "total_milestones": 8
  }
}
```

只有 `total_milestones > 0` 时管理台才显示百分比。没有正式里程碑时只展示章节、目标和场景摘要。

## 内容边界

```json
{
  "content_boundaries": {
    "character_death": "ask",
    "player_conflict": "consent",
    "romance": "fade_to_black",
    "horror": "moderate",
    "sexual_content": "blocked",
    "safety_pause": true
  }
}
```

世界规则可在副本详情中覆盖。安全暂停会冻结全部计时，且玩家不必公开原因。

## 自动 NPC

```json
{
  "npc_policy": {
    "enabled": true,
    "max_new_per_turn": 3,
    "generated_requires_review": true,
    "archive_after_inactive_rounds": 12
  },
  "context_budget": {
    "recent_turns": 6,
    "memories": 6,
    "active_npcs": 6,
    "ledger_items": 8
  }
}
```

模型生成 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆之一。自动 NPC 只能写公开资料、已知事实、误解和运行状态，不能创建系统级私密提示词。

`v0.9.3` 会按用途编译上下文：建卡预设、职业数值、开场选项和事件池不会进入每轮叙事；选项生成只接收当前场景、精简角色、最近事件、允许属性和风险—DC。`context_budget` 应用于新副本的运行快照，数值越高并不等于叙事质量越高；长期事实应进入记忆或故事账本，而不是无限增加最近回合。

## 开场四选一

```json
{
  "opening_choices": [
    {
      "key": "A",
      "text": "谨慎观察现场",
      "risk": "safe",
      "requires_check": false,
      "collective": false
    },
    {
      "key": "B",
      "text": "询问公开信息",
      "risk": "safe",
      "requires_check": false,
      "collective": false
    },
    {
      "key": "C",
      "text": "借助绳索翻越断桥",
      "risk": "desperate",
      "requires_check": true,
      "collective": false,
      "check_type": "standard",
      "check_stat": "敏捷",
      "difficulty": 15,
      "known_consequences": "失败可能坠落并与队伍分离",
      "advantage_sources": ["装备：绳索"],
      "disadvantage_sources": []
    },
    {
      "key": "D",
      "text": "带领全队离开当前区域",
      "risk": "controlled",
      "requires_check": false,
      "collective": true
    }
  ]
}
```

必须恰好包含 A、B、C、D，且至少一项为 `safe`。选项只能声明行动意图，不能提前保证结果。影响全队的行为必须设为 `collective: true`。

## 世界脉冲事件

```json
{
  "event_pool": [
    {
      "id": "late-courier",
      "title": "迟到的信使",
      "description": "负伤信使带来一条新线索和可响应威胁。",
      "weight": 2,
      "minimum_round": 2,
      "cooldown_rounds": 4,
      "once": true,
      "severity": "standard",
      "conditions": {
        "locations": ["边境无名酒馆·大厅"],
        "required_facts": ["酒馆保持中立"],
        "excluded_facts": ["信使事件已经解决"],
        "minimum_players": 2,
        "maximum_players": 4
      }
    }
  ]
}
```

每个完整多人轮次最多抽取一次。抽取结果先落库，模型失败重试不会换事件。

## 时间规则

JSON 内部仍以秒保存；`null` 或 `-1` 表示不限时，`0` 非法。管理台可直接选择秒、分钟、小时或天。

```json
{
  "time_rules": {
    "card_code_ttl_seconds": 1800,
    "card_draft_ttl_seconds": 604800,
    "card_completion_timeout_seconds": 86400,
    "preparation_timeout_seconds": 86400,
    "ready_timeout_seconds": 1800,
    "turn_timeout_seconds": 600,
    "turn_reminder_seconds": 180,
    "max_consecutive_timeouts": 2,
    "standby_timeout_seconds": 604800,
    "delegation_ttl_seconds": 86400,
    "check_timeout_seconds": 300,
    "vote_round_one_seconds": 600,
    "vote_round_two_seconds": 300,
    "vote_reminder_seconds": 120,
    "all_idle_pause_seconds": 600,
    "pause_stops_clock": true,
    "announce_timeouts": true,
    "turn_timeout_action": "skip",
    "card_timeout_action": "standby",
    "ready_timeout_action": "standby"
  }
}
```

`max_consecutive_timeouts: -1` 表示永不自动转候补。模型请求和数据库锁属于技术超时，不能设为不限时。

## 安全退场与返场

```json
{
  "safe_exit_templates": [
    "{character}去追查一条只能由其本人确认的线索，并留下未来重新联络的记号。"
  ],
  "return_rules": {
    "allow_return": true,
    "allow_resurrection": false,
    "requires_vote": true,
    "requires_story_condition": true
  }
}
```

模板必须中性，不羞辱、不擅自杀死角色，也不消耗团队公共物品。

## 初始状态

```json
{
  "location": "边境无名酒馆·大厅",
  "time": "雨夜",
  "scene_summary": "酒馆刚开门，尚无事件发生。",
  "facts": ["酒馆保持中立"],
  "inventory": {},
  "relationships": {},
  "check_modifiers": {
    "advantages": [],
    "disadvantages": []
  }
}
```

`check_modifiers` 可登记当前场景中明确、可验证的优劣势来源。模型不能直接改写权限、角色卡、会话阶段、骰点、投票或计时规则。

## 发布前检查

- 人数最小值与上限是否合理？
- 角色卡是否包含 `name`、`code`、背景、目标、信念、专长与知识边界？
- 属性预算、区间和加值表是否一致？
- A—D 是否只表达行动意图？
- 是否至少有一个安全选项？
- DC、风险和优劣势是否分别承担不同作用？
- 致命风险是否提前明示后果？
- 集体行为是否标记 `collective`？
- 自动 NPC 和上下文预算是否适合长期副本？
- 内容边界与安全暂停是否符合本团约定？
- 是否演练优势、劣势、灵感、集体检定、投票、超时、存档、克隆和模型失败？
