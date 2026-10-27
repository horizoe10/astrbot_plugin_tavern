# 通用世界包与 NPC 模板

本目录随 AI 酒馆 `v0.12.1` 发布，模板包版本为 `4.0.0`。这里的文件既供人工复制填写，也供 AI 在明确字段边界下生成或修改世界内容。

## 文件说明

| 文件 | 用途 | 当前接口 |
|---|---|---|
| `template-manifest.json` | 机器可读的兼容清单与发布同步规则 | 插件 v0.12.1 |
| `world-package.template.json` | 可直接通过严格体检的手动属性世界包 | 世界协议 v4、角色卡模板 v5 |
| `world-package-preset-stack.template.json` | 多个建卡预设共同生成属性的完整示例 | `preset_stack`、全组合体检 |
| `world-package-capabilities.template.json` | 可选能力定义、授予与成长变更 | 世界协议 v5、`capabilities@1.0` |
| `world-package-interaction-rules.template.json` | 不预设世界观语义的通用关系匹配 | 世界协议 v5、`interaction_rules@1.0` |
| `world-package-v5-full-example.json` | 五层运行时与全部可选模块综合示例 | 世界协议 v5、角色卡模板 v6 |
| `migration-v4-to-v5.example.json` | 先预检、再克隆应用的声明式迁移 | 不热改运行中副本 |
| `id-aliases.example.json` | 改名时保留稳定引用的别名表示例 | 导入阶段校验闭环和冲突 |
| `npc-import.template.json` | 可直接提交到常驻 NPC 导入入口的载荷 | NPC 导入模板 v2 |

模板文件只包含声明式 JSON，不能放入 Python、JavaScript、模板表达式、网络请求或任何可执行代码。

## 推荐使用顺序

1. 旧式世界复制 `world-package.template.json`；需要通用规则底座时复制 `world-package-v5-full-example.json`。
2. 修改 `slug`、`name`、简介、世界规律、角色卡预设、属性、检定策略、开场四选项与初始状态。
3. 在管理台执行世界包体检；必须消除全部错误后再导入。
4. 复制 `npc-import.template.json`，把 `world_slug` 改为已导入世界的 `slug`。
5. 为每个常驻 NPC 补全公开档案、真实知识边界、误解、能力、限制和私密导演信息。
6. 先导入世界包，再导入 NPC；最后在新副本中测试建卡、开场、检定消息顺序和 NPC 行为。

不要把模板文件本身当成正式世界长期维护。正式文件应使用新的稳定 ID 和内容版本，并保留自己的版本历史。

## 世界包必须修改的内容

- `slug`：世界稳定 ID，只使用小写字母、数字、下划线和连字符；发布后不要因改显示名而修改。
- `name`、`description`、`system_prompt`、`opening_scene`：替换全部示例文字，明确世界常识、能力来源、代价、上限、知识边界、因果连续性和玩家自主权。
- `world_content_version`：只表示该世界内容的版本，不等于插件版本或世界协议版本。
- `preset_sets`：为种族、职业、地区、身份、阵营、学院等预设分配不重复的稳定 `id`。建卡只会在进入对应字段时展示该预设源。
- `preset_dimensions`：声明性别、性格、职业、出身、信仰等通用维度，以及选择模式、顺序、前置、互斥和每项效果。
- `knowledge_boundary` / `knowledge_profiles`：定义世界知识硬上限和每种预设能够授予的知识档案。
- `content_boundary` / `content_profiles`：定义世界内容硬边界和预设附加限制；预设不得放宽世界限制。
- `fields`：每个字段的 `key` 必须唯一；选择字段使用 `preset_source` 或内联 `options`，不要把完整选项名单塞进标题或提示词。
- `stats`：选择 `none`、`manual`、`preset` 或 `preset_stack`。修改属性时同步更新检定允许属性、修正表、预算和相关选项。
- `resolution`：骰制必须已经注册；风险通过 `difficulty_policy` 映射 DC；结果档位由 `outcome_policy` 固定计算。
- `opening_choices`：必须提供 A—D。需要检定的选项必须在展示前拥有有效属性、风险、检定类型和已知后果。
- `initial_state`：写清地点、时间、场景摘要、公开事实、队伍物品、关系和已知修正来源。
- `capabilities`：顶层与 `rules.capabilities` 应保持一致，并与数值及裁定模式相符。

模板采用 `manual` 四属性作为容易改写的通用示例。若改为职业固定基础值与主/副属性加成，应参考内置阿尔维恩世界包的 `preset` 结构，并确保职业预设基础总值、加成选择和最终总值全部通过体检。

## 多预设属性自动结算（preset_stack）

需要由地区、身份、流派等多个选择共同生成属性时，请复制 `world-package-preset-stack.template.json`。规范配置位于 `rules.character_card.stat_generation`：

```json
{
  "mode": "preset_stack",
  "base_stats": {"body": 2, "agility": 2, "insight": 2, "presence": 2},
  "bonus_sources": ["species", "profession", "origin_region"],
  "bonus_source_rules": {
    "species": {"expected_bonus_total": 1},
    "profession": {"expected_bonus_total": 1},
    "origin_region": {"expected_bonus_total": 1}
  },
  "expected_total": 11,
  "min_per_stat": 2,
  "max_per_stat": 5,
  "allow_manual_edit": false
}
```

每个来源选项必须提供机器可读的 `stat_bonus`，例如 `{"stat_bonus":{"agility":1}}`。插件会从 `base_stats` 重新计算，绝不会在角色当前数值上继续累加；全部来源选完后自动保存最终属性与 `stat_generation_snapshot`，并跳过手动填写。修改任一来源后会清除旧结果并重算。

发布体检会验证来源字段存在且为单选、稳定 ID 不重复、属性 ID 与整数加成合法、每个来源的固定加成总量正确，以及所有合法组合的最终总和和单项范围。组合数超过 100000 会被拒绝，避免导入阶段产生不可控计算量。

## 步骤预设规则

预设是界面数据，不是提示词名单。插件只在当前字段解析：

```json
{
  "key": "origin_region",
  "label": "选择出身地区",
  "type": "preset_select",
  "preset_source": "origin_presets",
  "page_size": 5,
  "required": true
}
```

每个预设至少建议包含：

```json
{
  "id": "stable_origin_id",
  "value": "保存到角色卡的值",
  "label": "玩家看到的名称",
  "summary": "进入该步骤时显示的简短说明"
}
```

稳定 ID 负责长期识别；`label` 和 `summary` 可以随文案调整。需要条件字段时使用 `visible_when`，修改上游字段需要清理后续结果时使用 `clear_on_change`，互斥选择使用 `must_differ_from`。导入体检会检查无效预设源、重复 ID、空必填选项、非法字段引用和循环依赖。

## 机器骰制与文字 AI 边界

插件固定执行：

1. 玩家选择需要检定的选项。
2. 插件读取并锁定属性、风险、检定类型和 DC。
3. 注册骰制生成随机结果，插件计算加值和成功档位并持久化。
4. 先发送公开检定回执。
5. 再发送“后续内容正在生成中”。
6. 文字 AI 只依据锁定回执续写故事。

文字 AI 不得自行重投、修改 DC、改变结果档位或追加选项未公开的永久后果。故事重生成与崩溃恢复必须复用原检定回执。

## 上下文与生成速度

`v0.12.0` 会自动按任务裁剪上下文：故事生成不携带建卡预设、职业基础属性、开场选项或完整事件池；A—D 选项与选项修复使用独立的精简提示。启用 v5 能力模块时，叙事模型只收到当前角色可用能力的最小投影。模板默认采用最近 6 回合、6 条相关记忆、6 名活动 NPC 和 8 条故事账本。世界作者应把稳定事实放进 `system_prompt` 的简明世界常识、记忆或故事账本，不要把同一段设定同时复制到多个规则字段。

内部属性键建议继续使用英文或 ASCII 稳定 ID，例如 `charisma`；玩家可见名称使用中文 `label`，例如“魅力”。插件保存稳定 ID，并在选项、骰点与恢复展示时读取中文标签，不要把内部 ID 直接写进选项正文。

## NPC 模板字段

NPC 导入文件顶层必须是对象：

```json
{
  "world_slug": "目标世界稳定ID",
  "items": []
}
```

每个 NPC 必须有 `name`；`role` 默认可用 `npc`；`profile` 必须是对象；`prompt` 是叙事引擎可见的私密导演信息。建议保留：

- `identity`、`appearance`、`personality`、`public_background`
- `faction`、`location`
- `known_facts`：NPC 确实知道的事实，不是世界百科
- `misunderstandings`：NPC 合理但可能错误的判断
- `relationship_stance`：初始立场、合作条件与底线
- `capabilities`：来源明确的技能、资源与权限
- `limitations`：能力上限、知识盲区与资源条件
- `private_direction` / `prompt`：隐藏动机、秘密和透露条件

`v0.12.0` 的 NPC v2 导入优先按稳定 `slug` 更新，改显示名不会创建重复角色；只为兼容缺少 `slug` 的 v1 包才按名称回退匹配。v5 世界还会校验 `capability_refs`、`resource_refs`、`runtime_effect_refs`、`object_refs` 是否已在该世界注册。

## 交给 AI 修改时的要求

把本说明、`template-manifest.json` 和需要修改的模板一起提供给 AI，并使用类似要求：

```text
请基于随附模板生成一个完整世界包和 NPC 导入包。
保持 compatible_plugin_version、world_schema_version、角色卡模板版本及所有必需结构不变；
替换全部示例占位内容；所有预设、事件和 NPC 使用唯一稳定 ID；
选项与检定使用结构化字段，禁止把名单写入提示文本，禁止让文字 AI 生成随机骰点或修改 DC；
只输出合法 JSON，不省略未修改的必需字段，不加入任何可执行代码。
完成后逐项检查预设引用、属性引用、A—D、风险—DC、能力声明、NPC 知识边界和玩家自主权。
```

AI 修改后仍必须在实际插件管理台执行体检。AI 的文本检查不能替代运行时校验。

## 与插件接口同步更新

模板不是一次性附件，而是发布接口的一部分。每次出现以下改动，必须在同一个版本变更中更新模板：

- 插件版本、世界协议或角色卡模板版本变化。
- 建卡字段类型、预设源、条件依赖、分页、稳定 ID 或效果快照规则变化。
- 数值模式、属性引用、骰制注册、风险—DC、结果档位或检定回执结构变化。
- NPC 导入顶层载荷、必填字段、合并规则、公开/私密字段或角色类型变化。
- 世界体检、导入、冻结快照或迁移策略变化。

发布维护步骤：

1. 修改运行时代码与对应接口版本常量。
2. 更新 `template-manifest.json` 的兼容版本。
3. 更新两个 JSON 模板及本说明。
4. 更新 CHANGELOG 和当前版本验收基线。
5. 运行模板同步测试、世界包严格体检和完整测试。
6. 从最终 ZIP 解压后再次运行同一套验证。

测试会对照插件版本、世界协议、角色卡模板版本、NPC 导入模板版本与模板清单，并对通用世界包执行真实严格体检。任何版本不一致、模板缺失或结构失效都会阻止发布。
