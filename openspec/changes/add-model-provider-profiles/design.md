## Context

用户要求 CC Switch 式模型管理：自定义 endpoint + API key + 选模型。现有 `research/model_provider.py` 只有三种内置提供者；`research/store.py` 的设置校验按白名单字段校验；分析缓存键已包含 `model_identity()`；网络层 `research/network.py` 仅支持公网 GET。

## Goals / Non-Goals

**Goals**：多 profile 存储与激活、钥匙串密钥保护、有界模型 HTTP 客户端（含显式私网许可）、缓存隔离、IPC/UI/CLI 管理入口、一次性真实连接测试。

**Non-Goals**：代理多家供应商账号、密钥代管或转售、放宽任何分析确定性校验、流式输出、自动选择"最优"模型、为无 key 用户伪造可用状态。

## Decisions

1. **存储**：新表 `research_model_profiles(id, payload_json)`，与订阅同库；建表沿用现有"首次迁移前备份"机制。profile 字段：`name/protocol/base_url/model/timeout_seconds/allow_private_network/enabled`，另存 `key_configured/key_updated_at` 布尔与时间，不存密钥。激活指针放研究设置新字段 `model_profile_id`（null=环境变量提供者），沿用"运行中不得改设置"的现有约束。
2. **密钥**：复用 `guardian/token_store.py` 的钥匙串模式，新写 `research/model_key_store.py`：服务名 `com.alpha-guard.guardian`，账户 `model-profile:{id}`；后备文件位于用户配置目录 `model-keys/`（0700 目录、0600 文件、拒绝符号链接）。删除 profile 时同步删除密钥。
3. **协议**：`openai_compatible` POST `{base}/chat/completions`（非流式），`anthropic_compatible` POST `{base}/v1/messages` 带 `anthropic-version` 头。请求体只含 system/user 两条消息与 max_tokens；响应取首个候选文本，超限拒绝。
4. **网络**：新模块 `research/model_http.py`，不复用 `safe_fetch`（其为 GET、仅 80/443、无鉴权头）。校验：http/https、任意端口；默认解析 DNS 后仅允许全局单播地址并钉扎连接（防解析后漂移）；`allow_private_network=true` 时允许环回/私网但其余边界不变；单次 POST、不跟随重定向、响应 ≤2MB、超时与取消受控；错误只映射为低基数码。
5. **身份与缓存**：`model_identity()` 在 profile 模式返回 `profile:{protocol}:{sha256(base_url)[:12]}:{model}`；环境变量提供者身份不变，天然隔离。
6. **接入点**：`extract_text()` 增加 profile 分支；`model_capabilities()` 汇报当前生效提供者与 profile 状态。AnalysisBudget、取消、输出上限、引用校验全部不变。
7. **测试连接**：发送固定极小提示词（max_tokens 很小），只报告成败、耗时与低基数错误码；不写证据/报告；UI 先弹费用确认。
8. **IPC/UI/CLI**：复用 `research.command`，新增 `model_save/model_delete/model_activate/model_test` 字段集；快照带 `model_profiles` 与生效提供者。桌面研究页新增"模型"标签页；CLI 新增 `model-list/model-save/model-activate/model-delete/model-test`。

## Risks / Trade-offs

- 自定义模型能力参差：靠确定性校验兜底，弱模型如实失败；文档不承诺所有供应商可用。
- 私网许可可能被误开：默认关闭、界面显著标注、仅影响模型请求自身。
- keychain 后备文件是明文本地文件：权限 0600/0700，与 Guardian token 同级保护；文档如实说明。
- 真实第三方端点验收依赖用户自己的 key；无 key 时仅能以本机端点验证产品链路，不得宣称已接通该供应商。

## Migration Plan

1. 建表 + 密钥存储 + HTTP 客户端与离线测试。
2. 身份/缓存/能力接线，IPC、CLI、UI。
3. 本机端点端到端验证；用户 key 的真实供应商验证留待用户提供后执行。

## Open Questions

无。用户已确认按 CC Switch 式"endpoint + key + 选模型"方案实施；激活唯一、默认回退环境变量提供者。
