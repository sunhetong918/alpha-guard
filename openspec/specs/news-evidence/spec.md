# news-evidence Specification

## Purpose
TBD - created by archiving change add-on-demand-investment-research. Update Purpose after archive.

## Requirements

### Requirement: Collection outcome
The system SHALL 区分来源关闭、成功无新内容、部分成功和采集失败。
#### Scenario: All failed
- **WHEN** 所有已启用来源都失败
- **THEN** 本轮为 failed，不记录正常成功或“没有新闻”
#### Scenario: Empty success
- **WHEN** 来源成功返回零条
- **THEN** 该来源为 success 且数量为零
#### Scenario: Deduplicate before delivery limit
- **WHEN** 排名前五项已通知但后续有新项
- **THEN** 新项仍可进入本轮发送额度

### Requirement: Durable evidence
The system SHALL 保存内容身份、版本、原URL、作者、发布时间、获取时间、完整度及质量问题，并在修改已有数据库schema前备份。
#### Scenario: Same content
- **WHEN** 重复采到未变化材料
- **THEN** 复用内容和提取结果，不伪造新发布时间，不重复形成新报告事件
#### Scenario: Revision
- **WHEN** 同一来源内容发生修订
- **THEN** 保存新版本并使依赖旧版本的分析明确可追溯

### Requirement: Safe bounded content access
The system SHALL 校验外部URL及每次跳转、拒绝私网/本地/凭据URL、限制响应体与超时，并将原文视为不可信数据。
#### Scenario: Local address or redirect
- **WHEN** 导入地址或跳转解析到本地/私网
- **THEN** 拒绝请求并返回脱敏原因
#### Scenario: Oversized or malicious content
- **WHEN** 内容超过限制或包含要求泄漏凭据/执行操作的指令
- **THEN** 截止采集或按数据处理，不执行原文指令
