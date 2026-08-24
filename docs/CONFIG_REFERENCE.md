# AI 酒馆配置参考（自动生成）

> 本文档由 `tools/build_config_reference.py` 从 `_conf_schema.json` 自动生成。修改配置项后请重新运行该工具以同步。

## security · 安全与群范围

权限与群范围

- **admin_ids**（列表）：酒馆管理员 ID
  - 说明：只有这些真实平台用户 ID 可以开启、暂停、继续、关闭、读档或回滚。请填写字符串形式的 QQ 号/OpenID。
  - 默认值：`[]`

- **allowed_group_ids**（列表）：允许群 ID
  - 说明：可留空；已配置的管理员首次在目标群发送 /酒馆 开启 时会自动识别并加入，然后显示副本选择。也可手动填写群号或平台群 OpenID。
  - 默认值：`[]`

- **require_group_whitelist**（布尔）：强制群白名单
  - 默认值：`True`

- **unauthorized_command_behavior**（字符串）：未授权管理命令
  - 默认值：`silent`
  - 可选值：`silent`（静默拦截）、`deny`（回复无权限）

- **public_status**（布尔）：参与者可查看状态
  - 默认值：`True`

## model · 叙事模型

叙事模型

- **provider_id**（字符串）：固定聊天模型
  - 说明：留空则使用当前群会话所选模型。
  - 默认值：—

- **fallback_provider_1_id**（字符串）：备用聊天模型 1
  - 说明：主模型不可用时首先尝试；留空可跳过。
  - 默认值：—

- **fallback_provider_2_id**（字符串）：备用聊天模型 2
  - 说明：按编号依次回退；留空可跳过。
  - 默认值：—

- **fallback_provider_3_id**（字符串）：备用聊天模型 3
  - 说明：按编号依次回退；留空可跳过。
  - 默认值：—

- **fallback_provider_4_id**（字符串）：备用聊天模型 4
  - 说明：按编号依次回退；留空可跳过。
  - 默认值：—

- **image_caption_provider_id**（字符串）：图片转述模型
  - 说明：用于把带 jg 前缀消息中的图片转成客观文字，再交给叙事模型。留空时带图行动会被明确拒绝。
  - 默认值：—

- **image_caption_prompt**（长文本）：图片转述提示词
  - 默认值：`请用简洁、客观的中文描述图片中与角色行动、场景、物品、人物姿态和可见文字有关的信息。不要猜测看不见的事实。`

- **max_images_per_turn**（整数）：单回合图片上限
  - 说明：只读取带 jg 前缀的当前玩家消息中的图片。
  - 默认值：`4`

- **temperature**（浮点数）：叙事随机度
  - 说明：建议 0.4—0.5，可减少结构修复与额外模型请求。
  - 默认值：`0.5`

- **max_tokens**（整数）：单次最大输出 Tokens
  - 说明：100—300 字故事与四组选项通常不需要过高输出上限。
  - 默认值：`1400`

- **request_timeout_seconds**（整数）：模型超时秒数
  - 默认值：`120`

- **json_repair_attempts**（整数）：结构修复次数
  - 默认值：`1`

## runtime · 运行规则

运行规则

- **default_world_slug**（字符串）：新副本默认世界包
  - 说明：仅作为控制台建立副本时的初始选择；/酒馆 开启 仍要求明确选择副本。
  - 默认值：`aelvion-ashen-crown`

- **trigger_prefix**（字符串）：剧情触发前缀
  - 说明：默认 jg。只有“前缀 + 空格 + 内容”才会进入酒馆叙事与记忆，普通群聊完全忽略。
  - 默认值：`jg`

- **command_triggers**（列表）：命令触发词
  - 说明：自定义列表会完全取代 /酒馆；最多 8 项，每项最多 16 个字符，且不得与剧情触发前缀相同。第一项用于运行时提示文案。
  - 默认值：`['酒馆']`

- **two_phase_checks**（布尔）：严格两阶段行动判定
  - 说明：模型先申请检定，插件生成不可篡改的骰点，再由模型叙述结果。
  - 默认值：`True`

- **max_input_chars**（整数）：玩家单次输入上限
  - 默认值：`2000`

- **max_output_chars**（整数）：单次回复上限
  - 默认值：`5000`

- **enforce_mobile_output**（布尔）：手机端短篇输出约束
  - 说明：开启后每回合故事正文强制 100—300 字，每个选项（含括号）最多 50 字。
  - 默认值：`True`

- **recent_turns**（整数）：带入最近回合数
  - 说明：默认 6；长期事实由记忆与故事账本补充。
  - 默认值：`6`

- **memory_limit**（整数）：每轮检索记忆数
  - 说明：默认 6；锁定事实仍按世界策略优先进入上下文。
  - 默认值：`6`

- **user_cooldown_seconds**（浮点数）：同一玩家冷却秒数
  - 默认值：`1.5`

- **auto_snapshot_interval**（整数）：自动快照间隔回合
  - 默认值：`5`

- **card_review_notification_mode**（字符串）：角色卡审核通知方式
  - 说明：角色卡审核完成后，选择向成员发送结果的渠道。
  - 默认值：`both`
  - 可选值：`both`（私聊和群聊同时通知）、`group`（仅群聊 @ 成员）、`private`（仅私聊通知）

- **ooc_prefixes**（列表）：场外发言前缀
  - 默认值：`['【OOC】', '[OOC]', 'OOC:']`

### time_rules

全局时间与流程默认值

> 世界模板可覆盖这些值，新建副本会复制快照；留空或 -1 表示不限时，0 非法。

- **card_code_ttl_seconds**（整数）：私聊建卡码有效秒数
  - 默认值：`1800`
- **card_draft_ttl_seconds**（整数）：角色卡草稿保留秒数
  - 默认值：`604800`
- **card_completion_timeout_seconds**（整数）：等待完成建卡秒数（-1 不限时）
  - 默认值：`-1`
- **preparation_timeout_seconds**（整数）：准备大厅最长秒数（-1 不限时）
  - 默认值：`-1`
- **ready_timeout_seconds**（整数）：玩家准备确认秒数（-1 不限时）
  - 默认值：`-1`
- **turn_timeout_seconds**（整数）：个人回合秒数
  - 默认值：`-1`
- **turn_reminder_seconds**（整数）：回合提前提醒秒数
  - 默认值：`-1`
- **max_consecutive_timeouts**（整数）：连续超时转候补次数
  - 默认值：`-1`
- **standby_timeout_seconds**（整数）：候补自动退场秒数（-1 不限时）
  - 默认值：`-1`
- **delegation_ttl_seconds**（整数）：代控授权有效秒数（-1 不限时）
  - 默认值：`-1`
- **vote_round_one_seconds**（整数）：第一轮集体投票秒数
  - 默认值：`-1`
- **vote_round_two_seconds**（整数）：第二轮集体投票秒数
  - 默认值：`-1`
- **vote_reminder_seconds**（整数）：投票提前提醒秒数
  - 默认值：`-1`
- **all_idle_pause_seconds**（整数）：全员无互动后自动暂停秒数（留空关闭）
  - 默认值：`-1`
- **pause_stops_clock**（布尔）：暂停期间停止计时
  - 默认值：`True`
- **announce_timeouts**（布尔）：在群内公布超时与提醒
  - 默认值：`False`
- **turn_timeout_action**（字符串）：个人回合超时处理
  - 默认值：`hold`
  - 可选值：`skip`（跳过并累计超时）、`hold`（保留行动权）
- **card_timeout_action**（字符串）：建卡超时处理
  - 默认值：`remind`
  - 可选值：`standby`（转候补）、`release`（释放席位）、`remind`（仅提醒）
- **ready_timeout_action**（字符串）：准备超时处理
  - 默认值：`remind`
  - 可选值：`standby`（转候补）、`remind`（仅提醒）

## advanced · 高级设置

高级设置

- **audit_retention_days**（整数）：审计日志保留天数
  - 默认值：`90`

- **store_model_payloads**（布尔）：保存原始模型结构
  - 说明：排错用途，可能增加数据库体积；默认关闭。
  - 默认值：`False`

- **debug**（布尔）：调试日志
  - 默认值：`False`

## token_quota · Token 配额默认值

Token 配额默认值

- **enabled**（布尔）：默认启用配额
  - 说明：开启后，新建副本若尚未配置任何 Token 配额策略，将按本组默认值自动创建策略；关闭则保持“无配额不限量”的运行时行为（已有策略不受影响）。
  - 默认值：`False`

- **window_seconds**（整数）：滚动窗口秒数
  - 说明：配额统计的时间窗口，例如 86400 = 按最近 24 小时累计。
  - 默认值：`86400`

- **token_limit**（整数）：窗口内 Token 上限
  - 说明：窗口内累计消耗超过该值将拒绝新的模型请求。
  - 默认值：`400000`

## world_market · world_market

世界包远程市场

- **enabled**（布尔）：启用远程市场
  - 说明：开启后，市场视图会在本地条目之外合并拉取 remote_manifest_url 指向的远程条目。启用即代表你信任该注册表源（内容审核由注册表维护方负责，插件只做技术校验）。默认关闭。
  - 默认值：`False`

- **remote_manifest_url**（字符串）：远程清单地址
  - 说明：指向一个 JSON 清单文件（条目含 package_key/url/sha256/size 等字段），如 https://raw.githubusercontent.com/{owner}/{repo}/{ref}/worlds/manifest.json。
  - 默认值：—

- **allowed_hosts**（列表）：允许的远程主机白名单
  - 说明：仅允许从这些主机拉取（防 SSRF）；留空则仅允许 raw.githubusercontent.com、github.com、objects.githubusercontent.com、codeload.github.com。
  - 默认值：`['raw.githubusercontent.com', 'github.com', 'objects.githubusercontent.com', 'codeload.github.com']`

- **cache_ttl_seconds**（整数）：清单缓存秒数
  - 说明：清单与已拉取包的缓存时长，缓解 GitHub raw 限流。
  - 默认值：`600`

- **max_package_bytes**（整数）：单包体积上限
  - 说明：超过该字节数的远程世界包将被拒绝（防滥用）。
  - 默认值：`2000000`

- **verify_sha256**（布尔）：强制哈希校验
  - 说明：开启后，远程包必须匹配清单中的 sha256 才会被接受。
  - 默认值：`True`

## auto_backup · auto_backup

自动备份

- **enabled**（布尔）：启用自动备份
  - 说明：开启后，插件按固定间隔在 data_dir/exports 下导出完整备份 ZIP（bundle.json + catalog.sqlite3 + 各群独立存档），并只保留最近 keep_count 份。默认关闭。
  - 默认值：`False`

- **interval_hours**（浮点数）：备份间隔（小时）
  - 说明：两次自动备份的最小间隔，最小 1 小时，最大 720 小时（30 天）。
  - 默认值：`24`

- **keep_count**（整数）：保留份数
  - 说明：自动备份只保留最近 N 份，更早的会被自动清理（手动导出的备份不受影响）。
  - 默认值：`7`

## webhook · webhook

Webhook 事件通知

- **enabled**（布尔）：启用 Webhook 通知
  - 说明：开启后，酒馆内部事件（回合/世界/会话/备份等）会以 POST JSON 推送到下方地址，默认全部事件；可填写 events 过滤。
  - 默认值：`False`

- **urls**（列表）：推送地址列表
  - 说明：每个地址都会收到事件推送。仅支持 HTTPS/HTTP POST，请求体为 JSON 对象，包含 event / at / data 字段。
  - 默认值：`[]`

- **secret**（字符串）：签名密钥（可选）
  - 说明：填写后，每次推送会附加 X-Tavern-Signature: sha256=<hex> 头（对请求体做 HMAC-SHA256），接收方可据此校验来源。
  - 默认值：—

- **events**（列表）：事件类型过滤（留空＝全部）
  - 说明：只推送列出的 type（如 turn、session、world、backup、settings、vote、inspiration、character）；留空推送全部。
  - 默认值：`[]`

- **timeout_seconds**（浮点数）：推送超时（秒）
  - 说明：单次推送的最长等待时间，默认 10 秒。
  - 默认值：`10`
