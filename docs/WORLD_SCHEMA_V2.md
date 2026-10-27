# AI 酒馆世界包协议 v2（兼容）

`world_schema_version: 2` 要求世界明确声明角色数值、程序裁定和选项标注能力。AI 酒馆 v0.9.3 继续完整读取协议 v2；需要多个预设共同生成属性时，请升级到[世界包协议 v3](WORLD_SCHEMA_V3.md)。缺少版本声明或协议 v1 的世界包仍会被拒绝。

## 核心模式

- `rules.character_card.stats.mode`: `none`、`manual`、`preset`。
- `rules.resolution.mode`: `none`、`narrative`、`dice_only`、`attribute`。
- `none + attribute` 为非法组合；无属性世界可以使用 `dice_only`。
- `preset` 通过 `preset_selector` 选预设，通过 `bonus_choices` 叠加加成，通过 `total_validation` 校验基础与最终总值。

## 选项契约

选项正文放在 `text`，危险度放在 `danger_id`，检定放在 `check`。正文禁止自行拼接括号。危险度和检定彼此独立。

```json
{"key":"A","actor_id":"participant_x","text":"查看桥墩符文","danger_id":"controlled","check":{"required":true,"attribute_id":"perception","type":"standard","difficulty":12},"collective":false}
```

属性检定只能引用当前世界 `stats.attributes[].key`。未知属性默认拒绝；只有明确启用 `generic_check` 才能降级为通用检定。

## 字段类型

`text`、`textarea`、`integer`、`select`、`multi_select`、`boolean`、`derived`。选择字段必须提供 `options` 或 `options_source`。

## 导入校验

插件拒绝：协议版本过高、无数值世界启用属性检定、属性或预设 ID 重复、预设缺属性、基础总值/加成/最终总值不一致、未知危险度与未知检定属性。
