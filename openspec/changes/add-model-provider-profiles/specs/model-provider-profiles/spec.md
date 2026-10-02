# model-provider-profiles Specification（delta）

## ADDED Requirements

### Requirement: Profile storage and secrets
系统 SHALL 将模型 profile 持久化于本地研究库，API key 只存操作系统钥匙串（后备 0600 本地文件），数据库、IPC 与界面仅保存密钥是否已配置及更新时间。
#### Scenario: Save profile with key
- **WHEN** 用户保存带 API key 的 profile
- **THEN** 端点、协议、模型等非密钥字段入库；key 写入钥匙串；任何读取路径都不返回 key 明文
#### Scenario: Key store unavailable
- **WHEN** 钥匙串不可用
- **THEN** 回退到仅当前用户可读的本地文件；两种后端都失败时保存明确失败，不把 key 明文写进数据库或日志

### Requirement: Single active profile
系统 SHALL 最多允许一个激活 profile；未激活时分析使用环境变量提供者；切换激活不得开始任何采集或分析。
#### Scenario: Activate
- **WHEN** 用户激活一个已启用且密钥可用的 profile
- **THEN** 后续 AI 分析使用该 profile；既有环境变量提供者配置保持不变
#### Scenario: Missing profile
- **WHEN** 激活的 profile 被删除或停用
- **THEN** 分析回退到环境变量提供者；状态如实显示当前生效提供者

### Requirement: Bounded model endpoint requests
模型 HTTP 请求 SHALL 走有界客户端：单次 POST、不跟随重定向、请求与响应大小受限、受超时与取消控制；默认仅允许公网端点，只有 profile 显式允许私网时才可使用环回或私网地址。
#### Scenario: Private endpoint default rejected
- **WHEN** base URL 指向环回或私网地址且未显式允许私网
- **THEN** 请求被拒绝并返回低基数错误码，不发起连接
#### Scenario: Local inference allowed
- **WHEN** 用户在 profile 中显式允许私网并保存本机推理端点
- **THEN** 请求允许连接该端点；公网校验、大小与超时边界仍然生效

### Requirement: Honest provider failures
profile 模式的模型调用 SHALL 复用现有错误语义：只返回低基数错误码（超时、拒绝、响应超限、无效响应），密钥与原始响应不进入错误信息、数据库或界面。
#### Scenario: Provider rejects key
- **WHEN** 供应商返回未授权
- **THEN** 调用失败并显示受限错误码；key 不出现在任何输出中
#### Scenario: Deterministic gates unchanged
- **WHEN** 弱模型输出不满足引用或输出上限校验
- **THEN** 分析按现有规则拒收；不因自定义模型放宽任何质量门槛

### Requirement: Cache isolation
分析缓存键 SHALL 在 profile 模式下包含协议与 base URL 指纹；切换 profile 或端点后不命中旧缓存。
#### Scenario: Same model name different providers
- **WHEN** 两个 profile 使用同名模型但端点或协议不同
- **THEN** 各自的提取结果缓存互不复用，重复分析仍对相同输入零调用

### Requirement: Explicit connection test
系统 SHALL 提供"测试连接"命令：对指定 profile 发起一次极小真实调用并如实返回成败与受限错误码；界面在发起前明确提示该操作产生一次真实请求。
#### Scenario: Test succeeds
- **WHEN** profile 配置正确且供应商可用
- **THEN** 返回成功及耗时；不产生研究证据或报告
#### Scenario: Test refused by user
- **WHEN** 用户在费用提示上取消
- **THEN** 不发起任何网络请求
