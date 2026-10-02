# research-workspace Specification

## Purpose
桌面研究页、Guardian 鉴权 IPC 与 CLI 提供同一研究域的可操作入口：监控开关、订阅管理、证据与报告的离线查看、异步任务与真实状态展示；界面刷新只读取已保存结果，不触发联网分析，等待与失败状态可直接辨识。

## Requirements

### Requirement: User operated workspace
The system SHALL 在桌面研究页提供监控状态、单次/开始/停止、频率、市场/周期选择、订阅管理、来源状态、证据和报告详情。
#### Scenario: Read-only refresh
- **WHEN** 用户刷新研究页
- **THEN** 只读已保存数据和运行状态，不抓新闻、不调用模型
#### Scenario: Background work
- **WHEN** 执行耗时采集或分析
- **THEN** GUI保持响应且显示运行/完成/失败状态
#### Scenario: Incomplete content
- **WHEN** 订阅发现视频但未取得字幕
- **THEN** UI明确显示等待材料并提供补充入口
#### Scenario: Platform login
- **WHEN** 用户在已启用的专用浏览器模式点击打开平台登录
- **THEN** 通过鉴权IPC快速打开所选平台固定主页，保留登录页供本人完成登录，不开启扫描、不代填凭据，也不把打开网页声明为登录成功

### Requirement: Authenticated execution
The system SHALL 沿用Guardian鉴权IPC执行研究操作，UI不直接读取数据库或秘密配置。
#### Scenario: Unknown parameters
- **WHEN** 请求包含非法周期、频率、市场或未知字段
- **THEN** 拒绝操作，不开始外部调用
#### Scenario: Missing credentials
- **WHEN** 分析模型未配置
- **THEN** 用户仍可查看材料和来源状态，并看到尚未分析原因

### Requirement: CLI and acceptance
The system SHALL 提供对应CLI的单次/前台跟踪及离线结果读取，并将模拟与真实验收分开。
#### Scenario: Offline list
- **WHEN** 通过CLI列出证据和报告
- **THEN** 不触发采集或分析
#### Scenario: Completion claim
- **WHEN** 某平台仅通过fixture测试、尚未取得真实授权验证
- **THEN** tasks保留真实验收未完成，不报告该平台全部落地
