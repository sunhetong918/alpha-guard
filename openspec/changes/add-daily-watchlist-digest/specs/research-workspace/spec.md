# research-workspace Specification（delta）

## ADDED Requirements

### Requirement: Digest workspace surface
系统 SHALL 在桌面研究页提供每日摘要管理：开启/关闭、时刻、主题关键词、AI 摘要与投递开关、立即生成一次、最近摘要与历史查看；全部经鉴权 IPC 执行，CLI 提供同域命令与离线读取。
#### Scenario: Manage schedule
- **WHEN** 用户修改摘要设置
- **THEN** 保存后界面显示下一次运行时刻；读取或刷新不触发采集
#### Scenario: Immediate digest
- **WHEN** 用户点击立即生成一次
- **THEN** 执行一轮有界摘要批次并显示真实状态；不改变已设定的每日计划
#### Scenario: Offline history
- **WHEN** 用户翻看摘要历史
- **THEN** 只读取已保存记录，条目可链接到对应证据详情
