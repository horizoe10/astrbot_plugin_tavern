# 世界协议 v5

世界协议 v5 的设计原则是：插件规定安全、确定、可回放的执行语法，世界包定义这些语法在当前世界中的含义。任何模块都可选；一个纯叙事世界无需声明生命值、技能树、装备槽、元素关系或骰制。

## 协议信封

```json
{
  "world_schema_version": 5,
  "minimum_plugin_version": "0.11.0",
  "protocol": {
    "core_version": 5,
    "features": {
      "entity_registry": "1.0",
      "condition_engine": "1.0",
      "operation_engine": "1.0",
      "event_pipeline": "1.0",
      "resolution_receipt": "1.0"
    }
  },
  "required_features": [
    "entity_registry@>=1.0",
    "operation_engine@>=1.0"
  ]
}
```

`protocol.features` 只列出世界实际使用的模块。支持的可选模块包括 `capabilities`、`resources`、`runtime_effects`、`objects`、`resolution_methods`、`interaction_rules` 与 `action_intents`。

## 稳定引用

- 世界数据库主键、世界 `slug`、玩家可见 `WORLD xx` 编号和后台 `sort_order` 是四个独立概念。
- 包内实体使用 `type:stable_id` 引用，例如 `capability:flame_blast`、`resource:focus`、`custom:preset.profession`。
- 改名时使用 `id_aliases`，别名必须无环且不能一对多。
- 世界包只能读取 Entity Registry 中注册的引用，不能访问数据库字段路径。

## 五层执行模型

1. Entity Registry 注册实体、作用域与别名。
2. Condition Engine 用有限运算符读取注册值，记录实际读取结果。
3. Operation Engine 只执行白名单状态变更，支持 dry-run、数值策略和回滚差异。
4. Event Pipeline 在标准阶段匹配世界事件和交互规则，按优先级与叠加策略聚合。
5. Resolution Receipt 保存输入、规则版本、读取值、命中规则、步骤、结果与提交差异，并生成内容哈希。

世界包不得包含 Python、JavaScript、表达式模板、网络请求或其他可执行代码。

## 通用行动意图

```json
{
  "actor_ref": "character:example",
  "action_type": "world_defined_action",
  "capability_ref": "capability:optional",
  "targets": ["object:target"],
  "parameters": {},
  "declared_intent": "角色想实现的目标"
}
```

插件按顺序校验行动类型、能力可用性、目标、使用限制和资源成本；模型只能看到当前角色可用能力的投影。机械结果必须由插件结算，模型只根据公开凭证续写叙事。

## 通用交互规则

`interaction_rules` 不代表固定的“属性克制”。世界可以声明力量压制灵巧、身份关系、环境影响、线索权限或纯叙事提示，也可以完全不启用。规则模式为 `mechanical`、`narrative` 或 `hybrid`，效果仍须使用 Operation Engine 白名单。

## 运行中副本与迁移

- 已创建副本始终使用 `instance_configs.world_snapshot_json` 中的冻结契约。
- 世界编辑不会热替换运行中副本。
- 管理员可以先比较冻结契约与候选世界；存在已删除属性、预设、角色字段或不兼容数值模式时阻止克隆升级。
- 通过预检后可创建关闭状态的新分支并绑定候选世界快照；原副本、原存档和原裁定凭证不变。

完整示例见 `templates/world-package-v5-full-example.json`，能力、交互、别名与迁移的拆分模板位于同一目录。
