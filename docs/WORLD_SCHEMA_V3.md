# AI 酒馆世界包协议 v3

协议 v3 在 v2 的角色卡、机器骰制、风险—DC 和 A—D 选项契约上，增加多个建卡预设共同生成角色属性的 `preset_stack` 模式。AI 酒馆 v0.9.3 同时接受协议 v2 与 v3；旧世界无需升级即可保持原行为。

## 版本声明

使用新模式的世界必须同时声明：

```json
{
  "world_schema_version": 3,
  "minimum_plugin_version": "0.9.3",
  "rules": {
    "world_schema_version": 3
  }
}
```

这样 v0.9.2 及更早版本会因不支持协议 v3 而拒绝导入，不会把未知模式静默当成手动填点。

## 数值模式

- `none`：没有角色属性。
- `manual`：逐项填写并按预算校验。
- `preset`：单个职业基础值，加主属性与副属性固定加成。
- `preset_stack`：基础值加多个预设来源的 `stat_bonus`，由插件自动结算。

## preset_stack 配置

`rules.character_card.stats.mode` 设为 `preset_stack`，并在同级角色卡中声明：

```json
{
  "stat_generation": {
    "mode": "preset_stack",
    "base_stats": {
      "ti": 5,
      "yu": 5,
      "min": 5,
      "shi": 5,
      "jin": 5
    },
    "bonus_sources": [
      "origin_region",
      "social_identity",
      "martial_flow"
    ],
    "bonus_source_rules": {
      "origin_region": {"expected_bonus_total": 2},
      "social_identity": {"expected_bonus_total": 1},
      "martial_flow": {"expected_bonus_total": 3}
    },
    "expected_total": 31,
    "min_per_stat": 5,
    "max_per_stat": 9,
    "allow_manual_edit": false
  }
}
```

`bonus_sources` 引用真实的单选建卡字段 key，不依赖字段顺序。每个来源的每个选项都必须包含非空 `stat_bonus`：

```json
{
  "id": "origin_jiangnan",
  "value": "江南",
  "label": "江南",
  "stat_bonus": {"min": 1, "shi": 1}
}
```

属性键必须出现在 `stats.attributes` 中，加成必须是整数。显示名称可以修改，结算使用稳定字段 key、预设 ID 与属性 ID。

## 结算与快照

权威公式为：

```text
最终属性 = base_stats + 每个 bonus_sources 当前选项的 stat_bonus
```

插件每次都从 `base_stats` 重新计算，不在已保存属性上追加。所有来源完成后，角色卡保存：

- `stats.raw`：检定读取的最终属性；
- `stats.modifiers`：由最终属性映射出的检定修正；
- `stat_generation_snapshot`：基础值、来源字段、选项稳定 ID、显示名和当时加成；
- 角色资料中的对应派生字段，供恢复和审核核对。

修改任一来源会清除旧派生结果并重算。`allow_manual_edit=false` 时不会出现逐项数值填写，也不能使用“重填数值”直接修改结果。

## 导入体检

发布前体检会拒绝：

- 基础属性未完整覆盖全部属性键；
- 来源不存在、重复或不是单选字段；
- 选项没有 `stat_bonus`；
- 未知属性、布尔值或非整数加成；
- 来源加成总量不符合 `bonus_source_rules`；
- 任一合法组合的总和不等于 `expected_total`；
- 任一组合的单项值超出允许范围；
- 合法组合数超过 100000。

体检枚举全部合法组合。《潮痕无字》7×9×10 共 630 种组合会被逐一验证。

## 兼容与迁移

协议 v2 的 `none/manual/preset` 行为不变。正在运行的副本使用冻结世界快照，更新世界模板不会热替换旧角色卡。

旧角色迁移时先按现有预设复算：若最终属性一致，可以只补来源快照；若不一致，必须标记为管理员确认，不能静默覆盖。检定永远读取当前已批准角色卡中持久化的最终属性，不在每次投骰时重新读取世界预设。

