## Context

用户要求每日定时热点推送：按自选股票或板块，从"看视频、搜集资料"的既有能力中聚合。现有 `ResearchService._scan()` 已实现新闻源 + 订阅采集与证据入库；`GuardianSupervisor` 是常驻调度宿主；notifier 已有 Telegram/WhatsApp 投递；规范规定连续监控意图不持久化。

## Goals / Non-Goals

**Goals**：显式开启的每日计划（跨重启生效、缺口诚实）、关注清单确定性匹配与排序、有界生成、门禁化可选 AI 摘要、可选投递、桌面/CLI 入口、离线历史。

**Non-Goals**：全网热度抓取或排名、任意网页爬取、盘中实时推送、自动交易信号、为摘要开启浏览器录音/转写、追补错过的历史摘要。

## Decisions

1. **调度**：新模块 `research/digest.py` 的 `DigestScheduler`：常驻线程睡眠至下一个设定本地时刻；到点先取 `ResearchLease`（与扫描互斥），拿不到则 15 分钟内有界重试至多 4 次，仍在记录中说明后放弃本轮。设置存 `research_digest_settings` 单行表 + 摘要记录存 `research_digests(id, created_at, payload_json)`。`guardian/service.py` 在 IPC 服务启动后按设置恢复计划；`research.command` 的 `digest_save` 即时启停计划。监控 worker 的"运行意图不持久化"不改变：摘要计划是显式用户授权的独立计划，规范已按此修改。
2. **关注清单**：证券复用研究设置 `instruments`（含 name），主题为摘要设置中的 `topics: [{label, keywords(≤8)}]`（≤20 组）。匹配域为标题+摘要+正文前 2000 字符；证券命中 ticker 或名称（≥2 字符）；命中即分组。
3. **排序（确定性）**：score = 证券命中×3 + 主题命中×2 + 24h 内发布×1 + 跨来源佐证（`source_cluster` 计数）×2；并列按发布时间新→旧、证据 ID 字典序。同输入同输出，无随机性。
4. **生成**：单轮有界：新闻源沿用 `_scan` 的采集路径（条数上限、来源状态汇总），订阅 discover 不带 `media_handler`（明确不录音不转写）；条目 upsert 证据后从库读当前版本参与排序；只取本轮窗口内（上次摘要以来或 24h，取更宽）新见证据。输出记录含：统计、各分组条目（证据 ID、标题、URL、发布时间、命中原因）、覆盖缺口说明（ Guardian 离线跨过计划时刻 / 某分组为空 / 来源失败）。
5. **AI 摘要**：`ai_summary` 开关 + 提供者可用（环境变量或激活 profile）。一次调用，输入仅分组标题与证据标题/摘要；要求输出 JSON `{sections:[{key, summary}]}`；校验：每节 summary 中的数字必须出现在该组证据标题/摘要文本中、不得出现未提供的证券或主题；不通过整段拒收，回退确定性文本，不重试。提示词声明外部内容为数据非指令，沿用现有注入防护。
6. **投递**：`notify` 开关默认 false；开启时组装 ≤3500 字符文本经 `deliver_mobile(kind="news")` 发送一次，结果码记入记录；不因投递失败重发。
7. **入口**：`research.command` 新增 `digest_save/digest_once`；快照 `snapshot()` 增加 `digest` 块（设置、runtime 状态含 next_run_at、最近摘要摘要视图）。桌面新"每日摘要"标签页；CLI `digest-status/digest-configure/digest-run/digest-show/digest-history`。
8. **立即生成**：`digest_once` 不改计划，单批执行，与计划批次共用执行函数与互斥。

## Risks / Trade-offs

- 常驻摘要线程与用户手动扫描互斥靠 lease；冲突时摘要让位并记录，不阻塞用户操作。
- AI 摘要质量受模型影响：数字锚定校验兜底；无模型时功能仍完整（确定性摘要）。
- "每日一次"在电脑睡眠时会错过：如实记录缺口，醒来后下一时刻再执行，不追补。
- 投递通道费用/频次：每日最多一次，用户显式开启。

## Migration Plan

1. 表结构与设置校验、匹配/排序纯函数及离线测试。
2. 调度器、生成执行、AI 门禁、投递、IPC/CLI/UI。
3. 本地真实验证：真实 RSS 订阅 + 真实时刻语义 + 立即生成；通知通道未配置时验证"仅应用内"路径。

## Open Questions

无。用户已确认按"自选股票 + 板块关键词、每日定时、默认应用内、可选推送"实施。
