# market-research-profiles Specification

## Purpose
为美股、港股、A股提供研究市场档案：证券身份包含交易所且不跨市场合并，行情数据保留提供者时间与质量标记；A股先接入明确标注交易日的日线研究，数据时效或交易约束不合格时短线建议明确不可用。

## Requirements

### Requirement: Selectable market identities
The system SHALL 支持 US/HK/CN 研究市场及 USD/HKD/CNY 币种，证券身份独立于公司身份。
#### Scenario: Cross listing
- **WHEN** 同一家公司在不同市场上市
- **THEN** 研究报告分别标注证券、市场和币种，不能混用价格
#### Scenario: A-share selection
- **WHEN** 用户选择 CN
- **THEN** 通过CN研究适配器获取数据，不能当作已有US/HK硬规则配置生效

### Requirement: Market freshness
The system SHALL 保留提供者交易时间和获取时间，按市场与周期检查时效。
#### Scenario: Daily A-share data
- **WHEN** 只有已完成交易日的A股日线
- **THEN** 可用于合适的日线研究且短线能力标为不可用
#### Scenario: Unknown quote timestamp
- **WHEN** 提供者报价缺少可信交易时间
- **THEN** 不以刚获取的时间伪装实时，不形成短线方向建议

### Requirement: Trading constraints
The system SHALL 显示对应市场的交易约束和规则版本，A股普通股票须考虑T+1及板块/证券状态差异。
#### Scenario: Intraday exit assumption
- **WHEN** 分析A股当日买入策略
- **THEN** 不假设当日可卖出，不把不可成交的涨跌停价格当作可执行价格
