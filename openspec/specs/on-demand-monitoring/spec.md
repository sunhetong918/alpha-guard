# on-demand-monitoring Specification

## Purpose
研究信息采集默认关闭，只能由用户显式启动：提供单次扫描、开始/停止跟踪与频率选择，停止或后台进程重启后不自行恢复，并对每轮采集设定成本与并发边界。旧的四小时隐式新闻任务不再自动注册，股票规则巡检的只读行为不因此扩大。

## Requirements

### Requirement: Explicit activation
The system SHALL 默认不注册新闻研究周期任务，首次启动、重启和读取界面均不得触发外部采集。
#### Scenario: Default and restart
- **WHEN** 用户打开应用或 Guardian 重新启动
- **THEN** 研究监控为 STOPPED，订阅和已有报告可读，零新闻/作者/模型外部请求
#### Scenario: One scan
- **WHEN** 用户点击立即扫描一次
- **THEN** 只启动一轮有界采集和分析，不注册后续任务

### Requirement: Start and stop
The system SHALL 提供显式开始、停止与 1–1440 分钟频率选择，运行状态不作为重启自动授权。
#### Scenario: Start
- **WHEN** 用户开始跟踪
- **THEN** 创建一个调度任务并返回真实运行状态及下次检查时间
#### Scenario: Stop during work
- **WHEN** 用户在采集过程中停止
- **THEN** 取消后续调度，当前有界请求收尾后不得继续新请求、分析或通知
#### Scenario: Repeated start
- **WHEN** 用户重复点击开始或一次扫描
- **THEN** 同一研究任务不会并发重复执行且界面报告繁忙状态

#### Scenario: Close desktop during research
- **WHEN** 研究运行或启动请求尚待确认时，用户关闭桌面窗口
- **THEN** 明确提供继续后台、停止研究后关闭、取消选项；选择停止后须取得停止状态回执才关闭，失败保留窗口；未知状态只读取本地状态，不触发采集

### Requirement: Bounded execution
The system SHALL 限制单批材料数量、模型调用次数及请求时长，未变化材料不重复付费分析。
#### Scenario: Offline or quota exhausted
- **WHEN** 来源不可达、额度用尽或电脑恢复运行
- **THEN** 明确显示失败/覆盖缺口并有界重试或补采，不无限并发追赶
