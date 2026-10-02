# creator-subscriptions Specification

## Purpose
TBD - created by archiving change add-on-demand-investment-research. Update Purpose after archive.

## Requirements

### Requirement: Persistent author subscriptions
The system SHALL 支持添加、列出、启停、删除指定作者或授权feed，并保留平台、作者身份和独立来源状态。
#### Scenario: Add author
- **WHEN** 用户提交支持的作者主页或频道地址
- **THEN** 保存订阅和能力状态，添加本身不会启动自动监控
#### Scenario: Poll an enabled subscription
- **WHEN** 用户已启动跟踪或单次扫描
- **THEN** 自动发现该作者新内容并按稳定内容ID去重
#### Scenario: Pause
- **WHEN** 用户暂停订阅
- **THEN** 后续扫描跳过该作者且保留历史材料

### Requirement: Discovery and completeness are separate
The system SHALL 分别显示更新发现与全文/字幕获取状态，支持 metadata、summary、article、transcript 及完整度。
#### Scenario: Video metadata only
- **WHEN** 只有标题或视频简介
- **THEN** 显示等待内容，不显示“访谈已分析”或方向性建议
#### Scenario: Platform authorization missing
- **WHEN** 任意博主接口不支持或所需授权缺失
- **THEN** 保留订阅并显示 unsupported/permission_required/needs_configuration，不能伪造成功
#### Scenario: Supported authorized feed
- **WHEN** 用户为平台配置可合法使用的完整feed
- **THEN** 自动跟踪该feed并保留原平台及原文链接

### Requirement: Transcript evidence
The system SHALL 支持完整正文、字幕和已获准音视频转写，保留说话人、开始/结束时间及录制/发布时间的不确定性。

#### Scenario: 机器转写尚未核对
- **WHEN** 材料来自自动字幕或本地语音识别，尚未经人工核对
- **THEN** 保留机器转写标记和时间片段，允许形成有引用的观察报告，但不发布方向性投资候选
#### Scenario: Subtitle import
- **WHEN** 用户补充带时间戳的字幕
- **THEN** 存为可引用转写片段，并能关联原视频
#### Scenario: Transcription unavailable
- **WHEN** 无可用音视频访问或转写提供者
- **THEN** 显示具体等待配置状态，不用简介代替转写

### Requirement: Bounded opt-in player transcription
The system SHALL 在用户启用无字幕视频转写后，仅从正常访问且身份已核验的原视频播放器提取完整音轨，在本地转写，并支持停止和每轮额度限制。

#### Scenario: Explicit playback budget
- **WHEN** 用户开启无字幕视频转写并主动运行扫描
- **THEN** 先尝试原生字幕，每轮最多处理所设 1～5 条视频，单条上限为 60～600 秒；超限材料继续等待完整字幕或用户导入，不将片段标成全文

#### Scenario: Complete captured audio
- **WHEN** 原视频从起点按原速播放至原生结束，且源身份、媒体时长与转写片段时长均通过校验
- **THEN** 存入带原链接、实际发布时间和时间片段的机器转写证据；原始临时音轨处理后删除

#### Scenario: Source already transcribed
- **WHEN** 同一来源最新完整转写已保存且本次标题与发布时间未变
- **THEN** 复用该来源原证据，不重复播放或转写、不刷新原证据时间；新的内容版本或可见元数据变化触发重新获取

#### Scenario: Interrupted or inaccessible playback
- **WHEN** 用户停止、登录失效、视频切换、广告未结束、播放速度改变或音轨不完整
- **THEN** 停止本次处理、清理临时资源，保留已取得的元信息和此前证据，不能生成新的完整转写

#### Scenario: Text note contains unread images
- **WHEN** 小红书原始图文正文完整可读，但图片内容未提取
- **THEN** 保存正文及缺失图片内容标记，引用限于实际正文，研究结论降为观察与核验
