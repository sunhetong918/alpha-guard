# Alpha Guard 产品定位：可信沉默（Trusted Silence）

状态：产品与可靠性决策基线｜更新：2026-08-10

## Press Release

### Alpha Guard 让“没有提醒”也能被核验

个人投资者通常不缺行情、新闻和观点，真正缺少的是一套能长期执行自己规则、又诚实表达不确定性的守门系统。价格可能延迟，基本面字段可能缺失，来源会限流，定时任务也可能根本没有运行。传统脚本在这些情况下同样保持安静，用户无法分辨“条件没有触发”与“系统已经失明”。

Alpha Guard 是面向港股和美股个人投资者的自托管、只读、证据优先决策守门员。产品承诺叫“可信沉默 Trusted Silence”：只有预期扫描按时完成、启用范围被完整覆盖、规则依赖字段足够新鲜、账本完整，并且启用的送达链路可用时，系统才用 `GREEN` 表示沉默可信。

产品由两个相互独立但彼此约束的平面组成：

- Signal Plane 检查用户预先写下的规则，输出三态规则证据与五态人工核验决策；
- Silence Plane 检查扫描责任、数据新鲜度、provider 能力、送达和状态完整性，回答系统是否有资格保持安静。

Alpha Guard 不荐股，不预测短期涨跌，也不允许 AI 修改硬规则。可选的 Futu OpenAPI 集成只读取本机 OpenD 行情，不接收券商凭据、不解锁交易、不提交订单。`BUY_REVIEW` 和 `SELL_REVIEW` 只是人工核验类别。数据、新闻、AI 结果、颜色和提醒均不构成投资建议。

## 当前产品与未来产品

当前产品是本地优先的桌面系统：PySide 桌面 App 展示 Cockpit，后台 Guardian 在窗口关闭后继续调度并独占 SQLite/网络，Telegram 与 WhatsApp 是独立移动提醒，外部 dead-man heartbeat 可选。Typer CLI 继续提供配置校验、诊断、离线演练、Cockpit JSON 和显式修复。新克隆仓库默认离线、不联网、不通知，标的、新闻源、AI、Telegram、WhatsApp 和 heartbeat 都必须显式启用。

未来可以增加可选的只读 Cockpit Web，消费与桌面相同的四类收据。浏览器 UI 还没有实现；它不会成为远程交易面，也不能绕过 `UNKNOWN`、`AMBER` 或 `RED`。

## Cockpit 四问

`uv run alpha-guard status --json` 面向人和自动化回答四个问题：

1. 该跑的完整扫描跑了吗？
2. 当前沉默有完整、新鲜的证据吗？
3. 哪个 `provider × operation × market` 能力正在退化？
4. Telegram、WhatsApp 与外部 watcher 是否真正可用？

这与 [Google SRE 的监控原则](https://sre.google/sre-book/monitoring-distributed-systems/)一致：不仅要观察业务结果，还要能从外部确认监控链路本身仍在工作。Cockpit 展示事实和低基数原因码，不给投资评级。

## 八步用户旅程

1. 下载后用 `uv sync --frozen --extra desktop` 安装，先保持所有外部能力关闭。
2. 用 `alpha-guard-desktop --demo` 认识界面，再用 `validate` / `doctor` 检查真实本地配置。
3. 打开生产桌面；它连接或启动独立 Guardian，窗口关闭不终止后台守护。
4. 用 `dry-run` 在合成数据上理解三态证据、五态决策和消息渲染。
5. 只启用一个经过人工复核的标的，先执行不通知的真实 scan，并在桌面核对证据。
6. 配置 Telegram、WhatsApp 和可选 heartbeat，分别测试每条 delivery 链路。
7. 确认登录自启与 Cockpit 初始 full-scan 基线后，再允许 Guardian 连续运行和真实提醒。
8. 收到事故时先保留证据并按 Runbook 排查；只有账本损坏才执行最小 scope 的 `repair-state`。

完整操作契约见[可靠性与事故 Runbook](RELIABILITY.md)。

## 成功标准

### 安全与解释性

- 缺失、非有限、过期、future-skew、语义不明或 stale-if-error 的关键字段不会制造方向性结论；
- 每条提醒显示规则 ID、实际值、阈值、币种、event/source/observed 时间、来源和质量问题；
- 买卖方向冲突返回 `CONFLICT`，未知返回 `UNKNOWN`，两者都不伪装成确定建议；
- 动态外部文本受长度、schema、范围和 HTML 转义约束；AI 不能覆盖确定性规则。

### 克制与恢复

- 同一信号、同一证据在冷却期内不会重复刷屏，进程重启和并发扫描不破坏去重；
- 运行事故只发送边沿与恢复通知，不为每次失败重复打扰；
- `RED` 后必须先进入 `BLUE`，累计两次不同 observation ID 的完整健康扫描才恢复 `GREEN`；
- 状态损坏 fail closed，修复前自动备份，终端只展示 quarantine SHA-256，不回显原始载荷或 secret。

### 可用性

Silence Plane 采用 30 天 99% full-scan SLO：`error_rate = violations / expected`，`burn_rate = error_rate / 0.01`。样本计数始终与比例同时展示；没有预期窗口时不伪造成功率。SLO 告警以错误预算和可行动症状为中心，参考 [Google SRE Workbook 的 SLO 告警方法](https://sre.google/workbook/alerting-on-slos/)和 [Prometheus 告警实践](https://prometheus.io/docs/practices/alerting/)。

## FAQ

### 它是荐股软件或自动交易机器人吗？

不是。Alpha Guard 只评估用户自己预先写下的规则，输出人工核验提醒。项目不保存券商凭据、不提供订单接口、不执行任何交易；所有输出均非投资建议。

### `GREEN` 是否表示可以放心持有？

不是。`GREEN` 只表示 Silence Plane 有证据证明本轮扫描责任被可靠履行。它不评价资产质量、价格合理性或未来收益。

### `AMBER` 时为什么仍可能看到方向性提醒？

`AMBER` 表示完整覆盖不成立，而不是所有证据都无效。只要某条方向性规则所需字段独立满足来源、时间、币种和质量门禁，Signal Plane 可以继续生成对应人工核验提醒；系统同时禁止宣称完整可信沉默。

### 为什么 `RED` 后不能一次成功就恢复？

一次成功可能是偶然恢复、重放或旧基线。第一次 distinct full scan 只进入 `BLUE/RECOVERING`，第二次独立完整扫描才回到 `GREEN`，从状态机层面抵抗瞬时假恢复。

### 为什么需要外部 heartbeat？

进程自身无法在崩溃后发送“我已停止”。外部 dead-man watcher 通过缺失 ping 发现整条本地监控链路失联。heartbeat URL 具备认证能力，必须作为 secret；它只证明作业活性，不能替代字段新鲜度和完整扫描证据。

### 为什么选择桌面 App，而不是先做 Web 仪表盘？

桌面 App 可以维持单用户、本机 IPC 和系统 credential store 的边界，不需要开放 HTTP 端口。它只消费 Guardian 的可靠性收据，不重新定义颜色、SLO 或规则语义。未来 Web Cockpit 若出现，也不会加入交易动作。

### 数据源失败时为什么不总用旧缓存？

旧值可以帮助诊断，但不应在不知不觉中驱动方向性判断。stale-if-error 只在有限窗口内返回、明确标记为 stale，并 fail closed 为 `UNKNOWN`；用户仍能看到最近上下文，但系统不会据此制造买卖复核信号。

### 项目明确不做什么？

不做自动下单、收益承诺、黑箱价格预测、社交跟单、高频交易、券商写入或面向机构的全市场终端。公共托管、商业展示与数据再分发也不属于默认个人自托管边界，必须先取得相应数据与内容许可。
