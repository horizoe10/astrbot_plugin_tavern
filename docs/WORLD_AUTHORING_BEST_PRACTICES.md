# 世界包作者实践指南（Best Practices）

> 协议规范见 `WORLD_SCHEMA_V5.md` 与 `WORLD_AUTHORING.md`；本文聚焦「怎么写才稳、怎么排查」。

## 一、基本准则

1. **世界包只是数据，不是代码**。插件写死安全且确定的执行语法，世界包只声明「这些语法在当前世界代表什么」。不要尝试在 JSON 里表达逻辑。
2. **先过体检再发布**。任何世界包在导入前都会跑 `inspect_world_package`；本地写作用 `tests/test_v0110.py` 的严格体检用例做门禁。
3. **版本纪律**：
   - `world_schema_version`：协议结构版本（2/3/4/5），改动结构时递增；
   - `world_content_version`：内容版本（如 `3.0.0`），内容迭代时递增；
   - 控制台导入时按「内容版本新旧」判断覆盖或拒绝；同 slug 同版本但内容不同会触发冲突弹窗，不要静默覆盖。

## 二、常见陷阱

| 陷阱 | 表现 | 对策 |
|---|---|---|
| 属性 ID 与角色卡标签不一致 | 检定报「属性不属于世界或角色卡」 | 保证 `attributes[].key` 与角色卡 `stats.labels` 完全一致 |
| 检定 DC 超出难度区间 | 检定被夹取 | 用 `default_difficulty` 与 `difficulty_min/max` 约束范围内取值 |
| 全队行动选项超量 | 被自动降级为个人选项 | 每轮全队行动 ≤ `MAX_TEAM_CHOICES=2` |
| 开场场景过长 | 首回合上下文膨胀 | 开场控制在一屏内，细节交给 `initial_state` 的 facts |
| `preset_stack` 叠加顺序混乱 | 属性计算与预期不符 | `preset_stack` 始终基于 base 属性重算，保持确定性顺序 |
| 交互规则互相矛盾 | dry-run 命中冲突 | 用控制台 `worlds/simulate` 逐步试运行并查看裁定凭证 |

## 三、协议 v5 五层引擎速记

`Entity Registry → Condition → Operation → Event Pipeline → Resolution Receipt`

- 实体注册表：NPC、能力、物品的先验定义；
- 条件引擎：谁在什么情况下可以做什么；
- 操作引擎：规则裁定后的机械结果（扣血、给物、改状态）；
- 事件管线：开馆、回合、检定、表决等事件如何串行推进；
- 裁定凭证：每次机械裁定幂等落库，可回放、可审计。

## 四、性能与体积上限

- 单世界包建议 ≤ 200KB，实体数量克制；过大包会拖慢体检与模拟；
- `world_simulate` 已做 JSON 深度 / 解压体积限制，勿依赖超大 payload；
- 常驻 NPC 用独立文件（`*-npcs.json`）维护，避免主世界包臃肿。

## 五、调试建议

1. **先静态体检**（`worlds/preflight`），再**模拟试运行**（`worlds/simulate`），最后**真实开馆**；
2. 用「诊断报告」（`sessions/diagnostics`）核对会话状态、事件流与裁定凭证；
3. 需要回归防护时，把世界包加入 `tests/fixtures/` 并复用 `test_v0110` 的体检断言。
