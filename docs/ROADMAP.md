# Alpha Guard 演进路线：可信沉默（Trusted Silence）

更新：2026-08-10

路线只按信任依赖排序：先证明规则不会误导，再证明沉默可以被观察和恢复，然后改善只读体验，最后扩展授权证据。任何阶段都不以信号数量、回测收益或用户交易次数作为成功标准。

## Foundation：桌面值班台与可信内核（当前）

当前产品形态是 PySide 桌面 App、独立 Guardian、Typer CLI、SQLite、可选 Telegram / WhatsApp 与可选外部 dead-man heartbeat。默认配置不启用标的、新闻源、AI、通知或 heartbeat；`validate`、`doctor`、`dry-run` 和桌面 `--demo` 默认离线。

Foundation 已具备：

- Signal Plane：严格规则配置、字段来源、三态规则、五态人工核验决策、冲突抑制、跨重启去重和 claim lease；
- freshness：event/source/observed 时间语义、session-aware 价格门禁、future-skew、字段级预算和 fail-closed `UNKNOWN`；
- provider runtime：`provider × operation × market` timeout、bulkhead、幂等 retry、Full Jitter、`Retry-After`、circuit、fresh cache、有界 stale-if-error、Wilson 下界与 sample count；
- Silence Plane：`GRAY/GREEN/AMBER/RED/BLUE`、full-scan 责任、事故边沿/恢复通知、两次 distinct full scan 恢复；
- Reliability Cockpit：`status --json` 输出 schedule、silence、providers 和 delivery 四个视角，并计算 30 天 99% SLO、`error_rate` 与 `burn_rate`；
- 可恢复状态：SQLite integrity fail closed、备份优先、quarantine hash-only，以及显式 `repair-state --scope {global,market:US,market:HK,provider-runtime,run-log} --confirm`；
- 运行入口：不带通知的 `scan` / `news` / `run` 预览，以及双重授权的 `run --notify`。
- 桌面入口：生产默认通过鉴权本地 socket 连接/启动 Guardian，fixture 仅在显式 `--demo` 使用；Guardian 独占网络与 SQLite 写入。
- 移动送达：Telegram 与 WhatsApp 逐渠道 claim、逐渠道重试，Meta accepted 与 webhook delivered 语义分离。

Foundation 的完成门槛不是“偶尔能跑”，而是配置、缺失值、时间、并发、重放、损坏和恢复路径都有自动化测试，且 README、产品、架构与可靠性契约一致。

## Phase 1：连续运行与运维收口（下一步）

在不改变单用户、本地优先边界的前提下，进行真实 30 天 soak：验证交易日历期望窗口、SLO 计数、外部 watcher、Telegram/WhatsApp delivery、Guardian 登录启动与重启、provider 限流、网络抖动和 SQLite 备份恢复。

重点不是增加更多 alert，而是降低不可行动噪声：

- 基于多窗口 burn rate 校准告警阈值，保持事故边沿与恢复边沿语义；
- 用固定故障注入验证 timeout、bulkhead、retry storm、circuit half-open 和 stale-if-error；
- 固化 runbook 演练，验证每个 repair scope 只影响自己的责任范围；
- 对 data/news provider 的套餐、速率、个人使用、再分发与保留期限建立版本化许可记录；
- 建立人工标注的新闻回归集，持续验证关键词边界、AI 确定性降级与成本上限。

若引入指标 exporter，才按锁定版本的 OpenTelemetry HTTP semantic conventions 输出低基数指标；当前不能把未来 OTel 接入写成已部署。

## Phase 2：只读 Cockpit Web（未来，尚未实现）

当 `status --json` 与状态机契约稳定后，可增加本地只读 Web UI 或引导式 TUI。界面围绕 Cockpit 四问设计：

1. 该跑的 full scan 跑了吗？
2. 当前沉默有完整、新鲜证据吗？
3. 哪个 provider 能力在退化？
4. Telegram、WhatsApp 与外部 watcher 可用吗？

Web 只展示 schedule、silence、providers、delivery、规则证据与待核验事项，不远程控制券商、不重新计算状态、不允许一键清空事故。状态修复仍必须在本机 CLI 通过显式 `--confirm` 完成。

这一阶段可增加只读持仓导入、多通知渠道、配置向导、双语体验、财报和公司行动日历。数据库 ORM、API 服务或 PostgreSQL 只有在本地查询/迁移出现实测瓶颈后才评估。

## Phase 3：授权证据网络与规则回放（未来）

为关键指标接入多个有明确授权的 provider，展示口径、一致性与分歧；公司公告、财报、分红、拆股和交易所事件成为一等证据。规则回放只回答“当时是否会正确触发、证据是否完整、提醒是否过多”，不用于自动寻找最高收益参数。

若出现多用户或公开托管需求，必须先完成商业数据授权、新闻内容许可、隐私与法律评估，再讨论队列、PostgreSQL、分布式调度、灾备和多租户。免费个人数据源不能直接沿用到 SaaS，当前自托管许可边界也不能因 UI 上线而改变。

## 阶段门禁

任何新阶段都必须保持：

- `UNKNOWN`、`AMBER`、`RED` 不被 UI、AI 或 fallback 改写成确定方向；
- heartbeat URL、IPC token、Telegram/WhatsApp token、provider key 不进入日志、数据库、JSON 收据、启动项、截图或 issue；
- `GREEN` 只代表扫描责任履行，不代表市场安全或资产质量；
- 修复前备份、输出 hash-only，事故后用 distinct full scan 重建信任；
- 文档明确区分当前桌面 / Guardian / CLI / 移动渠道 / SQLite 与尚未实现的 Web / OTel / 多用户能力。

## 永久边界

Alpha Guard 是只读决策支持工具，不自动下单，不保存券商交易密钥，不承诺收益，不生成黑箱价格预测，不做社交跟单或高频交易。AI 不能修改规则、跳过新鲜度门禁或输出交易动作。所有提醒、颜色、评分和新闻标注均非投资建议。
