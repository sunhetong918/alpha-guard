## ADDED Requirements

### Requirement: Grounded extraction
The system SHALL 区分事实、作者观点、模型推断，对每个判断验证引用属于已保存材料并匹配原文。
#### Scenario: Invalid citation
- **WHEN** 模型返回不存在的证据ID、杜撰引文或未识别证券
- **THEN** 拒绝该结果并保留可见的质量问题
#### Scenario: Opinion
- **WHEN** 博主表达预测或观点
- **THEN** 保留观点属性，未经核实不能改写为公司事实

### Requirement: Horizon-specific recommendations
The system SHALL 对用户所选 short/swing/long 周期分别输出相关证券、逻辑、支持/反对证据、条件化行动、有效期及复核条件。
#### Scenario: Multiple horizons
- **WHEN** 用户选择多个周期
- **THEN** 同一证据生成分别标注周期的判断，不共用一个模糊买卖分数
#### Scenario: Insufficient or stale evidence
- **WHEN** 材料只有标题/摘要，行情过期或存在相反证据
- **THEN** 结果为观察/证据不足，不强制给买卖方向
#### Scenario: Numeric advice
- **WHEN** 报告包含价格或估值数值
- **THEN** 数值来自可核实输入并携带来源时间，不允许模型杜撰目标价或胜率

### Requirement: Cached extraction and current gates
The system SHALL 对未变材料缓存文本提取，但在形成建议时重新评估行情与有效期。
#### Scenario: Cached claim with expired quote
- **WHEN** 文本缓存命中但行情已过期
- **THEN** 复用提取结果并降级建议，不延长旧报价有效期
#### Scenario: Report expires
- **WHEN** 查看超过有效期的报告
- **THEN** 明确显示过期，不自动联网重算

#### Scenario: Source revises or withdraws evidence
- **WHEN** 同一来源观察到更新版本，包括标题修订或新的不完整材料
- **THEN** 当前展示的旧版方向建议降为观察并标明来源已更新；后续扫描只分析当前版本，历史原报告与事后跟踪保留原判断
