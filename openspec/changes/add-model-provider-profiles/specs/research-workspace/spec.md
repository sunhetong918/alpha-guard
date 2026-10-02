# research-workspace Specification（delta）

## ADDED Requirements

### Requirement: Model profile management surface
系统 SHALL 在桌面研究页提供模型 profile 管理（列表、新增/编辑/删除、启用、激活选择）与密钥输入，并通过鉴权 IPC 执行；CLI 提供同域命令。
#### Scenario: Manage without leakage
- **WHEN** 用户查看或编辑 profile
- **THEN** 界面显示密钥"已配置/未配置"而非明文；保存新密钥只更新钥匙串
#### Scenario: Cost warning before test
- **WHEN** 用户点击测试连接
- **THEN** 界面先提示该操作会产生一次真实模型请求，确认后才通过 IPC 执行并显示受限结果
#### Scenario: Active provider visible
- **WHEN** 研究页读取快照
- **THEN** 当前生效提供者（环境变量或激活 profile 的名称、协议、模型）与可用性状态可见；读取不触发网络请求
