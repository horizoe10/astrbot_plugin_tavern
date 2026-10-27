# 世界包 v0.12.0 接口与升级说明

## 协议基线

v0.12.0 当前世界协议为 v5，同时接受 v2、v3、v4。新世界建议使用：

```json
{
  "world_schema_version": 5,
  "minimum_plugin_version": "v0.12.0",
  "protocol": {
    "core_version": 5,
    "features": {
      "chat_experience": "1.0"
    }
  },
  "required_features": ["chat_experience@>=1.0"]
}
```

只有实际使用的功能才应放入 `required_features`。未知扩展字段会在导入、编辑和导出时尽量往返保留；插件不支持的必需功能版本会阻止导入，避免静默降级。

## chat_experience

该模块直接影响群聊体验，不只是作者元数据：

- `character_creation.primary`：主建卡通道，v0.12.0 推荐 `private_code`。
- `character_creation.fallbacks`：私聊不可用时的备用入口，如 `webui_token`。
- `multiplayer.spotlight`：聚光灯轮转策略，推荐 `round_robin`。
- `multiplayer.group_decisions`：公共资源、全队路线等是否进入 `vote`。
- `multiplayer.absent_player`：缺席玩家使用 `standby` 等策略。
- `safety.enabled`、`lines`、`veils`：安全暂停和内容边界。
- `continuity.recap_every_turns`：自动回顾间隔。
- `continuity.checkpoint_every_turns`：自动快照间隔，v0.12.0 运行时真实执行。
- `continuity.unresolved_threads_limit`：进入上下文的未决线索上限。
- `continuity.preserve_npc_intent`：跨回合保留 NPC 意图。
- `delivery.proactive_fallback`：`next_event`、`webui_only` 或 `discard`。
- `delivery.max_text_length`：按平台能力进一步取较小值分段。
- `dm.*`：叙事覆盖、秘密耳语、手工检定、状态干预权限。

WebUI 世界编辑器可视化编辑以上字段，保存前执行严格校验；后端仍会再次校验 DM 权限。

## 建议新增的世界属性

v0.12.0 已把对群聊提升最大的属性落入通用接口：多人聚光灯、投票、缺席策略、回顾/检查点、安全边界、投递回退和人工 DM 权限。世界作者还应充分使用 v5 已有的稳定实体 ID、能力/资源、运行时效果、物件、交互规则、知识边界、内容边界与行动意图，而不是把这些规则只写进自然语言提示词。

## 向后兼容

- v2–v4 缺少 `chat_experience` 时使用安全默认值，不改变源包。
- 旧世界继续使用数值型 `minimum_plugin_version`；v0.12.0 同时识别公开版本标识 `v0.12.0`。
- 运行中副本固定世界修订和快照；升级世界包只影响新副本或显式克隆/迁移。
- ID 别名迁移使用 `templates/id-aliases.example.json`，不得用显示名称猜测合并实体。
- v4 到 v5 参考 `templates/migration-v4-to-v5.example.json`，升级前先在世界管理页执行体检。

## 作者验收

导入前确认协议、必需功能、角色卡、预设组合、能力引用、资源范围、交互操作、NPC stable key、初始状态和四选项都通过体检。对多人世界再验证投票触发、缺席玩家、主动消息失败和人工 DM 权限四条路径。
