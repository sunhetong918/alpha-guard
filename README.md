# Alpha Guard — 可信沉默（Trusted Silence）

面向港股与美股个人投资者的自托管、只读、证据优先决策守门员。

Alpha Guard 把用户预先写下的价格、估值和质量规则转换为可核验的提醒。它也持续证明“为什么没有提醒”：只有预期扫描按时完成、启用范围被完整覆盖、规则依赖字段足够新鲜、状态账本可信，并且启用的送达链路可用时，沉默才是可信的。

> **安全默认值**：依赖安装完成后，刚克隆的仓库在运行时默认离线，不访问市场或新闻数据源，也不发送 Telegram、WhatsApp 或 heartbeat。示例标的、新闻源、AI 过滤、真实通知和外部 watcher 全部关闭，必须逐层显式启用。`uv sync` 本身仍需从软件包仓库下载依赖。

Alpha Guard 不是荐股软件，不承诺收益。券商连接是**可选且默认关闭的**：通过 Futu OpenAPI 集成（需 `--extra futu`、运行中的 OpenD 网关、显式配置三层开关）可启用实时行情与规则触发的自动下单，默认以 dry-run 模式只审计不下单；`live` 模式额外要求 `confirm_live: true` 人工确认。未启用时系统完全离线于券商，所有输出仍是“人工核验提醒”。一切输出均不构成投资建议。

## 为什么是“证据优先”

- 配置先验证：未知字段、未知规则、非法市场、非有限阈值和非正成本在扫描前失败。
- 缺失不是零：关键数据缺失、非有限或价格非正时，规则返回 `UNKNOWN`，不会制造方向性结论。
- 结论可解释：每条规则携带稳定 ID、实际值、运算符、阈值、单位和原因。
- 冲突不站队：相反方向的规则同时命中时返回 `CONFLICT`，抑制方向性提醒。
- 提醒有记忆：SQLite 保存信号状态、证据指纹、新闻指纹和运行记录，进程重启后仍可去重。
- 新鲜度按字段判断：event/source/observed 时间不会混用；价格门禁理解交易时段、future skew 与 stale-if-error。
- 沉默也有证据：Silence Plane 记录 full scan、覆盖、provider、送达、30 天 SLO 与事故恢复状态。
- 时间按市场表达：美股使用纽约时区、港股使用香港时区，并由交易所日历过滤周末和休市日。

当前完整形态由三部分组成：桌面 App 是只读值班台；后台 Guardian 独占调度、网络和 SQLite 写入；Telegram 与 WhatsApp 是相互独立的移动提醒渠道。关闭桌面窗口不会停止 Guardian。更完整的安装与安全边界见[桌面 App 与 Guardian 指南](docs/DESKTOP.md)，设计背景见[产品定位](docs/PRODUCT.md)、[目标架构](docs/ARCHITECTURE.md)和[可靠性契约](docs/RELIABILITY.md)。

## 快速开始

需要 Python 3.11 或 3.12，以及 [uv](https://docs.astral.sh/uv/)。无需先创建 `.env`，也无需申请任何 API 密钥，即可完成前三步离线检查。

```bash
git clone https://github.com/sunhetong918/alpha-guard.git
cd alpha-guard
uv sync --frozen

# 1. 严格校验规则与新闻配置
uv run alpha-guard validate

# 2. 查看各项能力是否就绪，以及缺失项影响什么
uv run alpha-guard doctor

# 3. 使用固定样例做离线演练；不联网、不通知
uv run alpha-guard dry-run
```

基础安装包含 CLI、规则引擎、SQLite、调度器、交易日历和 yfinance。按能力安装可选依赖：

```bash
# Telegram 通知
uv sync --frozen --extra notifications

# 桌面 App + Guardian；WhatsApp 使用基础 HTTP 依赖
uv sync --frozen --extra desktop --extra notifications

# Anthropic 新闻标注
uv sync --frozen --extra ai

# AKShare 港股报价与中文新闻
uv sync --frozen --extra cn-data

# Futu OpenAPI 实时行情与自动交易（可选，默认关闭）
uv sync --frozen --extra futu

# 一次安装全部可选能力
uv sync --frozen --extra all
```

启动桌面形态：

```bash
# 通常只需打开桌面；连接不到 Guardian 时会尝试启动它
uv run alpha-guard-desktop

# 运维或排障时，也可先在前台单独启动后台进程
uv run alpha-guard-guardian

# 完全离线的 UI 演示；不读取 SQLite、不联网、不启动 Guardian
uv run alpha-guard-desktop --demo
```

桌面 App 退出后 Guardian 继续守护。登录自启由设置页控制，macOS 使用当前用户的 LaunchAgent，Windows 使用当前用户的 `HKCU\\...\\Run`；它们都不需要管理员权限。平台细节、卸载和排障见[桌面指南](docs/DESKTOP.md)。

需要免 Python 的测试下载包时，可手动运行仓库的 `Desktop release artifacts` workflow，或推送 `v*` tag；它会产出 macOS arm64 的 `.tar.gz` 与 Windows x64 的 `.zip` Actions artifact。未配置签名 secrets 时产物会明确标记为 unsigned/ad-hoc test artifact，不等同正式签名发行版；签名、notarization 和 archive 布局见[桌面指南](docs/DESKTOP.md#原生下载包)。

CLI 脚本的等价调用方式是：

```bash
uv run python main.py validate
uv run python main.py doctor
uv run python main.py dry-run
```

如果项目位于 macOS iCloud Drive，且控制台入口报 `ModuleNotFoundError`，可能是 iCloud 给 `.venv` 内的 `.pth` 文件继承了 `hidden` 标志。可在项目根目录执行一次：

```bash
chflags -R nohidden .venv
uv run alpha-guard validate
```

这只修正本地虚拟环境的文件标志，不改项目数据；也可以直接使用上面的 `uv run python main.py ...` 等价入口。

## Futu OpenAPI 集成（可选，默认关闭）

Futu 集成提供两件事：**港股实时行情**（作为 AKShare 之外的首选价格源）与**规则触发的自动交易**。安全边界如下：

- 三层开关缺一不可：`uv sync --frozen --extra futu` 安装 SDK、`.env` 中 `FUTU_ENABLED=true`、`trading/futu.yaml` 中 `enabled: true`。
- 交易默认 `mode: dry`：订单意图、风控裁决与限价全部正常计算并写入审计，但**不发送任何真实订单**。
- `mode: live` 必须同时写 `confirm_live: true`，并至少配置一个 `auto_trade` 标的；配置校验会在扫描前拒绝不满足条件的安全违规。
- 风控闸（`trading/guard.py`）只在决策为 `BUY_REVIEW`/`SELL_REVIEW` 且方向与配置一致、价格可用、未触达单日限额与冷却时放行；`UNKNOWN`/`CONFLICT` 永不下单。
- 每笔订单（含被驳回的）都会产生不可变审计记录，并可经 `render_trade_alert` 推送到 Telegram/WhatsApp。
- 使用 `alpha-guard trade` 可完全离线地演练整条交易链。

运行交易需要本机 [OpenD](https://www.futunn.com/openAPI) 网关与开通 OpenAPI 权限的富途账户；行情与交易权限以富途官方为准。

## 命令

| 命令 | 用途 | 默认联网 | 默认通知 |
|---|---|---:|---:|
| `validate` | 严格解析 `signals/rules.yaml`、`news/config.yaml` 与 `trading/futu.yaml` | 否 | 否 |
| `doctor` | 诊断配置、可选密钥、SQLite 和下次调度时间 | 否 | 否 |
| `dry-run` | 用固定数据演练规则、证据和消息渲染 | 否 | 否 |
| `trade` | 离线演练交易链：规则→决策→风控→订单意图；不下单 | 否 | 否 |
| `rank` | 研究评分榜：基本面证据评分排序（`--fixture` 可离线）；不构成投资建议 | 有标的时 | 否 |
| `scan` | 单次扫描已启用的港股/美股标的；默认仅在终端预览 | 有启用标的时 | 否；需 `--notify` + 环境开关 |
| `news` | 单次扫描已启用的新闻源；默认仅在终端预览 | 有启用来源时 | 否；需 `--notify` + 环境开关 |
| `run` | 启动交易日历感知的长期调度器；默认预览模式 | 有启用任务时 | 否；需 `--notify` + 环境开关 |
| `status --json` | 离线读取 SQLite，输出 schedule / silence / providers / delivery 的 Cockpit 收据 | 否 | 否 |
| `repair-state --scope ... --confirm` | 先做 SQLite 备份，再显式隔离指定 scope 的损坏账本 | 否 | 否 |
| `alpha-guard-desktop` | 打开本地 Cockpit；`--demo` 为显式离线 fixture | 仅通过 Guardian | 否 |
| `alpha-guard-guardian` | 前台运行唯一后台 Guardian，供系统自启管理 | 取决于已启用责任 | 取决于已启用渠道 |

查看当前版本的参数与选项：

```bash
uv run alpha-guard --help
uv run alpha-guard scan --help
uv run alpha-guard status --json
```

`repair-state` 的 scope 只接受 `global`、`market:US`、`market:HK`、`provider-runtime` 或 `run-log`。它是账本损坏时的最后手段，不是清空提醒的日常命令。

`scan`、`news`、`run` 与 Guardian 都没有任何券商写入能力。即使 Telegram 或 WhatsApp 已启用，发送的仍然只是只读人工核验提醒。

## 从离线模式逐步启用

推荐按下面的顺序逐层打开能力。每一步之后先运行 `validate` 和 `doctor`，再进入下一步。

### 1. 启用一个监控标的

编辑 `signals/rules.yaml`，或从 `examples/rules.example.yaml` 复制后调整。仓库中的标的均为示例且 `enabled: false`。

```yaml
watchlist:
  AAPL:
    name: Apple Inc.
    market: US
    currency: USD
    enabled: true
    cost_basis: 175.0
    alert_cooldown_hours: 24.0
    sell_rules:
      - id: aapl-price-upper-review
        type: price_above
        value: 220.0
        note: 复核提供者报价、时间和质量问题
    buy_rules: []
```

配置约束：

- `market` 只接受 `US` 或 `HK`；对应币种分别为 `USD` 和 `HKD`。
- `enabled` 缺省为 `false`。
- 每条规则必须有稳定且唯一的 `id`；修改阈值时不要随意更换 ID。
- `value` 必须是有限数值；价格和 PE 阈值必须大于零。
- `price_drop_pct` 必须在 `(0, 100]` 内，并要求正数 `cost_basis`。
- `alert_cooldown_hours` 必须是非负有限数值。
- schema 是严格的，拼错字段不会被静默忽略。

当前支持的规则类型：

| 类型 | 判断 |
|---|---|
| `price_above` | 提供者报价 `>= value` |
| `price_below` | 提供者报价 `<= value` |
| `pe_above` | PE(TTM) `>= value` |
| `pe_below` | 正数 PE(TTM) `<= value` |
| `roe_above` | ROE 百分点 `>= value` |
| `price_drop_pct` | 相对 `cost_basis` 的跌幅 `>= value` |

### 2. 按需启用新闻源或 AI

编辑 `news/config.yaml`，或参考 `examples/news.example.yaml`。三个新闻源与 AI 默认都是关闭的。

```yaml
ai_filter:
  enabled: false
  alert_threshold: 3
  max_ai_calls_per_scan: 20

sources:
  finnhub:
    enabled: false
    lookback_hours: 6
  newsapi:
    enabled: false
    lookback_hours: 12
    extra_queries: []
    language: en
    page_size: 20
  akshare:
    enabled: false
```

Finnhub 与 NewsAPI 使用基础安装即可。启用 AKShare 来源前先安装 `cn-data` extra；启用 Anthropic 前先安装 `ai` extra：

```bash
uv sync --frozen --extra cn-data
uv sync --frozen --extra ai
```

启用 Finnhub、NewsAPI 或 Anthropic 后，再把对应密钥写入项目根目录的 `.env`。不要提交 `.env`：

```dotenv
FINNHUB_API_KEY=
NEWSAPI_API_KEY=
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

新闻处理先做确定性关键词匹配。英文和其他拉丁关键词使用单词边界，避免 `Fed` 命中更长单词的一部分；查询顺序稳定且会去重。

AI 只标注新闻影响，不参与硬规则，也不能生成交易动作。文章标题、摘要和来源被视为不可信外部文本，会先截断再放入提示词；模型返回值必须通过严格 JSON schema，评分只能为 1–5，方向只能是“利好 / 利空 / 中性”。

- `ai_filter.enabled: false`：不调用模型，保留关键词匹配结果并标记为 AI 已禁用。
- AI 已启用但没有密钥、调用超额或输出无效：确定性降级为关键词人工复核，并记录质量问题。
- AI 成功评分：只有达到 `alert_threshold` 的条目进入提醒列表。

### 3. 最后启用真实通知

复制环境变量示例，并只开启需要的渠道。Telegram SDK 位于 `notifications` extra；WhatsApp Cloud API 使用基础 HTTP 依赖：

```bash
uv sync --frozen --extra notifications
cp .env.example .env
```

```dotenv
NOTIFICATIONS_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

WHATSAPP_ENABLED=true
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_DEFAULT_TO=8613800000000
WHATSAPP_GRAPH_API_VERSION=v26.0
WHATSAPP_TEMPLATE_LANGUAGE_CODE=zh_CN
WHATSAPP_SIGNAL_TEMPLATE_NAME=alpha_guard_signal
WHATSAPP_INCIDENT_TEMPLATE_NAME=alpha_guard_incident
WHATSAPP_NEWS_TEMPLATE_NAME=alpha_guard_news
WHATSAPP_TRUST_TEMPLATE_NAME=alpha_guard_trust
```

Telegram 与 WhatsApp 分别显式授权、分别领取 delivery claim、分别记录结果：一个渠道成功不会掩盖另一个渠道失败，重试也不会重复发送已经成功的渠道。CLI 真实发送还要求命令显式带 `--notify`；缺少开关或必需配置会明确失败，而不是静默跳过。

```bash
# 单次股票扫描并允许发送人工核验提醒
uv run alpha-guard scan --notify

# 长期运行并允许发送人工核验提醒
uv run alpha-guard run --notify
```

Telegram 消息使用受限 HTML，所有动态内容都会转义，新闻链接只允许 `http://` 或 `https://`。WhatsApp 的 signal、incident 与 trust 模板必须先在 WhatsApp Manager 审批，news 模板可选；正常 Guardian 工作流发送模板消息。

WhatsApp 的 24 小时 customer service window 有严格语义：窗口外只能发送已批准模板；自由文本只有在调用方逐次确认窗口仍开放时才允许发送。Cloud API 返回 `2xx` 只表示 Meta **accepted** 请求，不表示手机已收到；`delivered`、`read`、`failed` 必须来自 Meta 的 message-status webhook。当前本地发送端只把 accepted 记为提交成功，不会把它展示成 delivered；若部署 webhook，必须在独立 HTTPS 入口校验 Meta 验证/签名并用 `wamid` 关联状态。参见 Meta 的[模板概览](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview)、[24 小时窗口说明](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages#customer-service-windows)和[消息状态 webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/status)。

不带 `--notify` 的 `scan`、`news` 和 `run` 仍可联网获取已启用来源的数据，但只在本地展示和记录结果。

### 4. 可选启用外部 dead-man watcher

本地进程崩溃后无法主动报告“我已停止”。如需从外部检测调度器或主机失联，可配置一个 heartbeat endpoint：

```dotenv
HEARTBEAT_ENABLED=false
HEARTBEAT_URL=<secret-ping-url>
HEARTBEAT_TIMEOUT_SECONDS=5
```

先在外部服务创建检查，再把 `HEARTBEAT_ENABLED` 改为 `true`。完整 `HEARTBEAT_URL` 具有认证能力，必须像 API key 一样保密：不要提交到 Git、放进命令行、粘贴到 issue 或截图。Alpha Guard 的日志、SQLite 和 `status --json` 只记录是否配置、时间与低基数错误码，不回显 URL。

heartbeat 只补足“程序还活着吗”的黑盒证据，不能替代字段新鲜度或 full scan。真实运行仍从双重授权入口启动：

```bash
uv run alpha-guard run --notify
```

Healthchecks 是可选实现之一；其[官方 Pinging API](https://healthchecks.io/docs/http_api/)说明 start/success/failure 语义，[Slug URL 文档](https://healthchecks.io/docs/slug_urls/)明确 UUID 或 ping key 应作为 secret。也可以使用满足同样安全边界的自托管 endpoint。

## 规则结果：三态证据与五态决策

单条规则只有三种结果：

| 状态 | 含义 |
|---|---|
| `TRIGGERED` | 有效数据满足阈值 |
| `NOT_TRIGGERED` | 有效数据明确不满足阈值 |
| `UNKNOWN` | 缺失、非有限、价格非正或依赖数据不可用，无法判断 |

卖出侧规则采用“任一命中”，买入侧规则采用“全部命中”。组合后只产生五种决策：

| 决策 | 含义 |
|---|---|
| `NONE` | 没有需要提醒的状态 |
| `BUY_REVIEW` | 预设的机会观察条件满足，需要人工核验 |
| `SELL_REVIEW` | 预设的风险观察条件满足，需要人工核验 |
| `UNKNOWN` | 至少一项关键规则无法评估，禁止制造方向性结论 |
| `CONFLICT` | 相反方向同时命中，抑制两边方向并要求人工排查 |

`BUY_REVIEW` 和 `SELL_REVIEW` 是内部兼容名称，不是交易指令。提醒文案不会要求用户在券商中执行操作。

## 可信沉默：Signal Plane 与 Silence Plane

规则的五态决策与可靠性的五种颜色是两套不同语义：

- **Signal Plane** 判断独立证据是否满足预设规则；
- **Silence Plane** 判断系统是否有资格声称“没有需要提醒的事项”。

Silence Plane 的颜色只表达运行保护状态：

| 颜色 | 状态 | 含义 |
|---|---|---|
| `GRAY` | `UNCONFIGURED` / `PAUSED` | 没有启用责任或用户显式暂停；不是普通未知 |
| `GREEN` | `HEALTHY` | full scan、覆盖、新鲜度与账本证据支持可信沉默 |
| `AMBER` | `DEGRADED` | 局部能力缺失；相关独立新鲜证据仍可提醒，但不能宣称完整沉默可信 |
| `RED` | `BLIND` | 完整静默不可相信，或关键 delivery / integrity 失败 |
| `BLUE` | `RECOVERING` | 事故恢复或新基线校准中；不是通用维护色 |

普通保护事故后，第一次具有新 observation ID 的健康 full scan 进入 `BLUE`，第二次 distinct full scan 才恢复 `GREEN`；重复 ID 或非完整扫描不计数。事故通知只发送状态边沿和最终恢复边沿，不会每轮重复刷屏。

当前 Reliability Cockpit 是 CLI 的机器可读收据：

```bash
uv run alpha-guard status --json
```

桌面 App 和 CLI 收据回答同样四问：该跑的扫描跑了吗、当前沉默可信吗、哪个 `provider × operation × market` 能力退化、Telegram / WhatsApp / external watcher 是否可用。Silence Plane 还计算 30 天 99% full-scan SLO：`error_rate = violations / expected`，`burn_rate = error_rate / 0.01`；没有预期窗口时结果为 `null`，不会伪造健康百分比。

当前交付形态是 PySide 桌面 App、独立 Guardian、CLI、SQLite、Telegram、WhatsApp 与可选 heartbeat。没有浏览器 Web 控制面，也不开放 TCP 端口。状态、字段新鲜度、provider 隔离、SLO、heartbeat 和事故 Runbook 的完整契约见[可靠性文档](docs/RELIABILITY.md)。

## 数据证据与评分

规范化快照包含 `provider`、`retrieved_at`、`as_of`、`currency`、`quality_issues` 与字段级来源。每个规则依赖字段分别记录 event/source/observed 时间语义；本地刚刚取得数据不能证明内容刚刚发生。价格在开市阶段按年龄预算判断，休市时按最近已完成交易 session watermark 判断；超过 future-skew 容差的时间直接不可用。

新鲜缓存可以参与规则。provider 失败时，有界 stale-if-error 只提供明确标记的人工诊断上下文，不能驱动方向性提醒；缺失、过期或时间语义不明统一 fail closed 为 `UNKNOWN`。免费来源中的“当前价”可能延迟，因此 Alpha Guard 将其称为“提供者报价”，不暗示交易所实时性。

外部调用按 `provider × operation × market` 隔离 timeout、bulkhead、幂等 retry、Full Jitter、受限 `Retry-After`、circuit、缓存和健康样本。Cockpit 同时展示真实调用的 sample count、成功率和 95% Wilson 下界；缓存命中不会虚增成功率，样本不足显示 `insufficient_data`。

基本面评分保留 100 分兼容输出，同时附带：

- `coverage`：参与评分的有效字段覆盖率；
- `confidence`：`high`、`medium` 或 `low`；
- `limitations`：缺失字段、异常口径和提供者质量问题。

覆盖率不足时不会给出强结论。“52 周价格位置”只是历史区间启发式，不是内在价值或安全边际估算。

## SQLite 去重与审计

默认状态库是本地 `.alpha-guard/state.db`。它不保存 API 密钥或券商信息，只保存最小运行状态：

- `signal_state` / `signal_events`：信号激活、复位、证据变化、成功通知和冷却时间；
- `news_seen`：已经成功通知的新闻指纹；
- `run_log`：任务名称、状态、开始/结束时间和脱敏结构化详情；
- protection / expected-window 账本：扫描责任、颜色、事故边沿、恢复证据与 30 天 SLO；
- provider runtime / delivery：熔断、缓存、低基数样本，以及 Telegram / WhatsApp / heartbeat 的脱敏送达状态。

同一信号首次激活、明确复位后再次激活、证据指纹变化，或正数冷却期结束后才重新具备提醒资格。`UNKNOWN` 不会把已激活状态错误复位；发送失败也不会被写成已通知，因此后续扫描仍可重试。`status --json` 从这些账本构造离线 Reliability Cockpit；`doctor` 展示本地数据库健康状态与下一次计划任务。

发送前会在 SQLite 中原子领取一个有时限的 claim；同一信号或新闻即使被两个进程同时扫描，也只有一个发送者能取得 lease。发送失败会主动释放，进程崩溃则在 lease 到期后恢复重试。

账本损坏时系统会把可信静默 fail closed，而不会自动删除或覆盖原始证据。确认已保留现场后，只对确实损坏的最小 scope 执行 `uv run alpha-guard repair-state --scope <scope> --confirm`。命令先用 SQLite backup API 生成带时间戳的本地备份，再把损坏载荷隔离到 quarantine，只输出备份路径和 SHA-256 审计摘要，不回显原始载荷或 secret。

真实 `BLIND` 事故以及 global/market protection-state repair 需要两次 distinct full scan，状态按 `RED → BLUE → GREEN` 恢复。合同代际变更或 `provider-runtime` / `run-log` quarantine 属于基线失效：状态为 `BLUE`，一次 post-epoch full scan 可重建 `GREEN`。零启用责任则诚实回到 `UNCONFIGURED/GRAY`；以 `status --json` 展示的恢复证据为准。

## 调度与交易日历

`run` 使用 APScheduler 和 `exchange_calendars`：

- 美股扫描：`America/New_York` 09:25，交易所日历 `XNYS`；
- 港股扫描：`Asia/Hong_Kong` 09:25，交易所日历 `XHKG`；
- 每日摘要：各市场本地时间 16:10；
- 新闻扫描：`Asia/Shanghai` 的 00:00、04:00、08:00、12:00、16:00、20:00。

本地市场时区会自然处理美股夏令时变化；周末和交易所休市日不会执行对应的市场时点任务。调度任务启用 `coalesce`、单实例和 misfire 宽限，降低进程暂停后集中补跑造成的提醒风暴。

## 数据、新闻与模型服务的使用限制

依赖库的开源许可证不等于其上游数据可以自由商用、再分发或公开展示。下面只是入口摘要，不是法律意见；条款和套餐会变化，部署前请重新阅读官方页面。

| 来源 | 本项目用途 | 需要注意 |
|---|---|---|
| [yfinance / Yahoo Finance](https://ranaroussi.github.io/yfinance/) | 美股行情与基本面；港股基本面和历史数据 | yfinance 文档说明其面向研究与教育，Yahoo Finance 数据 API 预期为个人使用；它不是授权交易所实时行情源 |
| [AKShare](https://github.com/akfamily/akshare) | 港股报价与东方财富中文新闻 | AKShare 代码采用 [MIT](https://github.com/akfamily/akshare/blob/main/LICENSE)，但项目声明数据仅供学术研究与参考；代码许可证不替代各原始网站的数据权利 |
| [Finnhub](https://finnhub.io/pricing) | 美股公司与市场新闻 | 官方套餐页将当前免费许可标为 Personal Use，并受套餐、频率和服务条款限制 |
| [NewsAPI](https://newsapi.org/terms) | 全球关键词新闻 | Developer 免费计划仅限开发和测试环境，不能用于 staging 或 production（包括内部生产使用） |
| [Anthropic API](https://support.anthropic.com/en/articles/8987200-can-i-use-the-anthropic-api-for-individual-use) | 可选新闻影响标注 | 受 Anthropic Commercial Terms、计费与数据政策约束；启用后，截断后的新闻文本会发送给外部模型服务 |

本项目默认面向个人、本地、自托管研究。任何公开服务、团队内部生产部署、数据再分发、商业展示或 SaaS 化，都需要自行取得适当的数据与内容授权，并完成隐私和法律评估。

## 开发与验证

```bash
uv sync --frozen --extra all --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy analysis data desktop guardian news notifier reliability signals state config.py scheduler.py main.py
```

项目锁定 Python 3.11–3.12，并通过 `uv.lock`、pytest、Ruff 和 CI 保持可复现性。

## 文档与安全

- [产品定位与 PR/FAQ](docs/PRODUCT.md)
- [桌面 App、Guardian、自启与移动提醒](docs/DESKTOP.md)
- [目标架构](docs/ARCHITECTURE.md)
- [可靠性契约与事故 Runbook](docs/RELIABILITY.md)
- [演进路线](docs/ROADMAP.md)
- [安全策略与漏洞报告](SECURITY.md)

发现疑似密钥泄漏、通知注入、越权联网、订单执行路径或其他安全问题时，请按[安全策略](SECURITY.md)私下报告，不要在公开 issue 中粘贴真实凭据。

## 免责声明

Alpha Guard 仅用于个人研究和规则复核。市场数据、新闻和 AI 输出都可能延迟、不完整或错误；任何提醒均不构成投资建议。使用者应独立核验数据、遵守上游条款，并自行承担决策责任。
