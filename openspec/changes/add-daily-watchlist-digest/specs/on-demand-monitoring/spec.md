# on-demand-monitoring Specification（delta）

## MODIFIED Requirements

### Requirement: Explicit activation
The system SHALL 默认不注册新闻研究周期任务，首次启动、重启和读取界面均不得触发外部采集；用户显式开启的每日摘要属于独立的已授权计划任务，是本条"不自行恢复"约束的明确例外，连续监控的运行意图仍不持久化。
#### Scenario: Default and restart
- **WHEN** 用户打开应用或 Guardian 重新启动
- **THEN** 研究监控为 STOPPED，订阅和已有报告可读，零新闻/作者/模型外部请求；已显式开启的每日摘要按其自身规范继续生效
#### Scenario: One scan
- **WHEN** 用户点击立即扫描一次
- **THEN** 只启动一轮有界采集和分析，不注册后续任务
