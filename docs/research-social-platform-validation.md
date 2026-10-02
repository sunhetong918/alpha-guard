# 抖音、小红书作者订阅接入验证

最近验证日期：2026-10-02。用户已确认在专用 Chrome 登录小红书、抖音、哔哩哔哩。以下区分登录后实测、历史限制和仍需核验的全文路径。

## 已实现的浏览器路径

`ALPHA_GUARD_BROWSER_ENABLED` 默认关闭。启用后，只有用户主动扫描或启动监控才访问作者页。默认 `ALPHA_GUARD_BROWSER_MODE=dedicated` 使用正常、可见的 Chrome 专用资料目录：

`~/Library/Application Support/alpha-guard/browser-profile`

不复制日常浏览器的 Cookie、登录数据库或浏览记录。Chrome 以自己的正常页面加载机制访问内容；不生成接口签名、不绕过登录或验证码。调试连接只指向 `127.0.0.1`，共享连接复用，单次读取创建并关闭自己的标签页，不枚举日常浏览器标签页。专用模式会正常前置本次标签页，让要求前台可见的平台完成正常渲染；因此扫描时用户会看到专用 Chrome 窗口。Guardian 退出释放调试连接，保留 Chrome 窗口和资料目录，以免中断用户登录。

`existing` 模式可连接用户主动开放调试权限的现有 Chrome；首次连接可能需要 Chrome 的授权确认。该模式不依赖开发用 CDP Proxy。

## 2026-10-02 登录后实测

所有访问使用产品 `BrowserPage`，在 `/tmp/alpha-guard-live-browser.lock` 上串行加锁，以免不同平台同时前置标签页。只关闭本次创建的内容页，不操作用户登录页。公开页面给出的原生链接可在内存中用于正常导航，带参数的链接不入库；小红书证据统一保存 `https://www.xiaohongshu.com/explore/<note_id>`，避免不同入口导致重复转写。

| 链路 | 实测结果与证据 | 解释范围 |
| --- | --- | --- |
| 小红书新闻晨报作者更新 | [作者页](https://www.xiaohongshu.com/user/profile/5c00a062000000000501b916) 返回 32 张有真实 ID 的作品卡片，前 20 条以视频为主。 | 登录前只有空 ID 的限制已经解除；不能把视频说明当全文。 |
| 小红书图文正文探针 | 正常点击第 27 张卡片 [图文笔记](https://www.xiaohongshu.com/explore/6abba491000000001c02e14b)，读取对应作者、发布时间及约 500 字原生完整描述。 | 证明图文正文可读；该旧内容的单页探针不替代产品前五条流程验收。图片未识别，保留 `visual_content_not_extracted`。 |
| 小红书实际订阅流程 | 正常 GUI 搜索找到 [第一财经有看投](https://www.xiaohongshu.com/user/profile/6729c907000000001c0186ab)，运行 `discover_social(max_content_items=5)` 后写入独立 SQLite。20 条更新有真实 ID，前五条取得真实详情时间和作者。 | 首条置顶图文虽有 49 字描述，却全部是话题标签。已经加入实质正文门槛，标签不会再升级为完整文章。其余视频仍须字幕或完整转写。 |
| 小红书实际订阅与完整音轨 | 最新财经讲话 [潘敏立：市场短期走势](https://www.xiaohongshu.com/explore/6abcd21c000000001c00decc) 经 `discover_social(max_content_items=2)` 身份校验后完整录音。时长 292.642 秒，`start=0`、`ended=true`、实际录制 292.750 秒，2,379,790 字节；正常播放器状态无错误。 | 120 秒测试预算会正确拒绝；改为产品允许的 600 秒上限后完整通过。本地 small 与无词表 turbo 转写及独立 SQLite 入库均完成；turbo 为 150 段、1,714 字。主要论述可读，仍有金融术语误识，金额未经人工核对，保持机器转写未核验。 |
| 抖音作者更新 | [中国网财经作者页](https://www.douyin.com/user/MS4wLjABAAAAq3kXlON6-IM-t8_aV2Rb6LHqdg1uCL5RF3LnZiszu2g) 返回 18 条真实作者作品。 | 只读作品列表，排除页脚推荐；读取正常页面已加载的作品响应核对作品和作者，不重放接口请求。 |
| 抖音完整讲话音轨 | [何小鹏讲话](https://www.douyin.com/video/7686727666610883903) 正常播放 31.672 秒，静音音轨捕获 257,729 字节。`start=0`、`ended=true`、最高播放时间 31.672 秒、实际录制 31.789 秒。 | small 转写产生 21 个时间片段，无词表 turbo 产生 19 段，均入独立验收库；与画面烧录字幕明显不符，现场样本质量不合格。平台结束后自动回到 0，不能用事后 `currentTime=0` 错判为没有播放，也不能忽略结束事件。 |
| 抖音无清晰语音样本 | [英伟达回购短片](https://www.douyin.com/video/7690773408996445480) 约 9.589 秒，音轨读取可用，但 base 识别仅得噪声文本，时间戳超出媒体。 | 已拒绝 `transcription_duration_mismatch`，不把背景音乐或画面文字伪装成访谈全文。需要用户补充原文或另行识别画面。 |

两条讲话已走完整音轨 → 本地 small/turbo 转写 → 独立 SQLite 保存，并验证重复音轨与来源缓存复用。较大模型对照已经结束，当前本机默认选择无词表 turbo，保留 base/tiny/small；没有“中文准确度已合格”的结论。抖音现场讲话仍不合格，小红书清晰讲话有部分改善但仍需核对。固定通用词表虽改善部分名称，却带来破损字符和时间片段缺口，因此未启用；自动转写及其缓存现在拒绝 U+FFFD 破损文本。原始识别结果不人工润色。完整质量记录见[本机转写验收](research-transcription-validation.md)。

本次验收材料、音轨回执与独立 SQLite 放在忽略版本管理的 `build/research-preview/` 中，不修改用户研究库。验收音轨与回执保留在该忽略目录供本机复核，不写入用户研究库或安装包；产品每轮录音使用临时目录并在结束或取消后清理，不建立媒体库。

## 2026-10-02 16:00 完整产品自动订阅复验：两平台通过

第二轮复验使用与此前完全相同的产品路径、时限与无词表 turbo 配置，目标按作者当前页动态选取，两平台全部通过：

| 平台 | 作者与目标 | 第一轮 | 第二轮 | 回执 |
|---|---|---|---|---|
| 小红书 | 第一财经有看投，[潘敏立：市场短期走势](https://www.xiaohongshu.com/explore/6abcd21c000000001c00decc)（292.6 秒，与早间目标相同） | 20 条更新发现；完整录音 + turbo 转写 150 段/1,714 字符立即持久化，380.5 秒 | 3.1 秒，零录音零转写，缓存命中，证据 ID/身份/哈希一致 | `build/research-preview/xhs-turbo-product-e2e-r3.json` |
| 抖音 | 认证作者第一财经，滕泰消费券讲话 `7592249603081227546` 及另一条 | 20 条更新发现；前两条各完整录音 + turbo 转写立即持久化（目标 17 段/204 字符），128.9 秒 | 7.0 秒，零录音零转写，两条缓存命中，证据稳定 | `build/research-preview/douyin-product-e2e.json` |

- 早间小红书两轮失败（详情 `browser_timeout`、录音准备 `media_unavailable`）本轮均未复现；失败回执原样保留，未放宽任何就绪或完整性门槛。
- 原抖音验收作者"中国网财经"主页本轮显示"用户不存在"（`douyin-author-page-diagnostic-r2.json`）；经搜索用户页换用认证作者第一财经，地址栏、页面作者、原生作品响应与广告标记校验照常执行。
- 抖音转写内容为真实财经讲话，可读但仍存在术语/数字误识（如"消费补贴"识别为"消费布贴"），`machine_transcription_unverified` 与"现场样本不能当逐字稿"的结论不变。
- 两平台独立 SQLite 分别为 `xhs-turbo-product-729b75390c.sqlite`、`douyin-product-e2e-a4f2f999bc.sqlite`；未写用户库、未加订阅、监控保持 STOPPED。

## 早间完整产品自动订阅验收记录（两次未通过，已由上节复验接续）

2026-10-02 04:06 UTC，另建独立 SQLite，调用真实 `connectors.discover` 和 `VideoResearch(max_items=2, max_seconds=600)`，读取当前已配置的无词表 turbo 命令。验收脚本只把 `discover_social` 的公开 `max_content_items` 参数限制为 2，以覆盖置顶图文及目标财经视频；来源、网页内容、录音和 ASR 均没有替身，也不使用此前保存的音轨代替本轮采集。

本轮作者发现成功返回 20 条作品，但目标 `6abcd21c000000001c00decc` 的详情页出现 `browser_timeout`，43.055 秒后返回 `partial / waiting_for_content`。录音尝试 0 次、ASR 0 次、缓存命中 0 次，目标保持 `metadata`。没有进入第二轮，也没有宣称跨轮缓存验收成功；遇到这次明确失败后未循环重试。原先分步取得的完整音轨、turbo 转写及独立入库结果仍是有效技术证据，但不能替代这次完整产品流程的结果。

首次失败报告保留为 `build/research-preview/xhs-turbo-product-e2e-attempt1-timeout.json`，独立库为 `build/research-preview/xhs-turbo-product-4c10b30f7b.sqlite`。用户库、订阅、监控状态未改动，已关闭本轮拥有的标签页。脚本 `build/research-preview/verify_xhs_turbo_product_e2e.py` 保存了实际调用与验收范围；本次单次超时的根因尚未确定。

随后只做一次无录音诊断，没有延长 25/40 秒原有上限，也没有再跑录音或 ASR。作者及前两条详情在 10.993 秒内全部完成，三次 `Page.navigate` 分别用时 0.646、0.523、0.538 秒；目标正文提取成功。安全可见状态未发现登录、验证码或访问频繁提示。最慢的 `Runtime.evaluate` 是额外安全状态读取，分别约 2.778 和 2.577 秒，均成功；读取该状态时播放器尚未挂载。记录仅含方法名、阶段、耗时和安全布尔状态，不含参数、原始 URL 或认证数据。日志位于 `build/research-preview/xhs-cdp-timeout-diagnostic.json`。

对比此前成功的 `capture_xhs_product.py`，作者 URL、公开 `max_content_items=2` 和底层 `discover_social/BrowserPage` 一致；本次完整验收增加了 `connectors.discover` 外层、真实 `VideoResearch` 和独立持久化。超时发生在 handler 进入之前，因此不能归因为 turbo 转写耗时。本次诊断未复现异常，而上一轮没有命令级日志，无法确定原超时来自导航还是正文调用，也不能据此断言是平台阻塞。未改源码、未再循环。

无录音诊断证明同一详情恢复后，又获准以完全相同的源码、时限、前两条范围与无词表 turbo 配置执行一次恢复验收。第二次作者及详情读取成功，真实进入 `VideoResearch → capture_audio`，但在原有 12 秒就绪等待内没有找到同时满足可见、有效时长和 `readyState >= 2` 的媒体元素，返回 `media_unavailable`。全轮 16.479 秒，采集尝试 1 次、ASR 0 次、缓存命中 0 次，未实际开始完整录音、未产生全文证据、未进入第二轮。这个失败已定位在录音准备阶段，区别于首轮未定位的 CDP 超时；未因此放宽播放器或完整性门槛，也未继续重试。

第二次报告为 `build/research-preview/xhs-turbo-product-e2e.json`，独立库为 `build/research-preview/xhs-turbo-product-76f0082bfa.sqlite`。当前不能将此作者“完整自动发现 → 录音 → turbo → 新库立即持久化 → 次轮零转写”标为通过，尽管此前分步调用已经完整采集和转写了同一视频。

为核对是否遗漏正常播放动作，随后仅做页面与播放就绪对照，没有再次录音或转写。真实页面截图显示视频封面和“加载中”；点击已核验的原视频封面后，媒体元素虽出现，但时长仍未知、`readyState=0`。原生 `xgplayer-play` 控件尚不可见，没有点击隐藏控件。最后一次在原有 40 秒页面上限内观察：原视频约在正文核验后 14 秒挂载；正常调用其静音 `play()` 后，直到约 35 秒仍为 `networkState=2`、`readyState=0`、时长未知，播放 Promise 未完成。此时无法把问题归结为漏点播放按钮，也没有证据支持放宽完整性门槛。根因仍未确定，等待用户在同一专用浏览器确认正常手动播放情况。

安全状态与截图位于 `build/research-preview/xhs-player-visible.json`、`xhs-player-visible.jpg`、`xhs-player-click-control.json`、`xhs-native-play-control.json` 和 `xhs-normal-play-deadline.json`。只记录作品公开页面、就绪状态与耗时，不保存媒体地址、认证参数或 Cookie。全部诊断标签页已关闭，用户登录页保留。

## 内容版本与重复扫描

证据版本包含标题、作者、发布时间的变化，正文内容哈希和跨来源内容簇仍独立复用。每个来源保留当前真实观察到的版本指针；后来只读到元信息时形成修订屏障，不回退使用旧完整转写。重新取得相同历史全文时可恢复指向该不可变版本，原发布时间和首次取得时间都不更新。分析历史记录不会移动当前来源指针。旧数据库首次增加当前版本表前自动备份，不改写历史证据 ID 或引用，重开不会重复备份。 当前报告如果依据的证据已被新版本或元信息屏障替代，会降为观察并标记 `source_evidence_superseded`，不延长报告有效期。冲突判断只比较当前来源版本，扫描也只选当前材料；历史报告和回放保留原判断。

## 正文与音轨完整性规则

小红书只有 `type=normal`、作者和笔记 ID 与来源一致、原生描述未截断，并且剔除话题标签后至少 40 个字符的真实正文，才可保存为文字组件的 `article/full`。只有标题、标签、视频简介或 DOM 摘要时保持元信息/部分材料。原始图片存在或是否存在未知时加 `visual_content_not_extracted`，用户核对图片前不能形成方向性行动建议。

视频采集只操作本次拥有的可见页面中的正常播放器。抖音在开始前核对地址栏作品 ID、页面作者链接、原生公开作品响应中的作品 ID/作者 ID 与非广告标记。录音使用标准 `HTMLMediaElement.captureStream()` 和音轨 `MediaRecorder`，不调用麦克风、屏幕捕获，不下载或重放媒体签名 URL，也不绕过跨域、DRM、登录或验证码。缺少浏览器能力或出现 `SecurityError` 时明确失败。

录音从 0 开始、静音、正常 1 倍速度，最长 600 秒/8 MB。必须自然结束，播放时间和墙钟时间一致；中途换元素、换源、时长变化、seek、倍速、取消或超预算均不标完整。只在完整回执通过后写出临时 WebM，读取分块受限。诊断仅包含安全枚举与标准媒体状态，不记录带签名 URL 或播放器错误原文。机器转写仍保留未核验质量门禁，不因成功转写就直接给出买入结论。

## 2026-09-30 登录前历史证据

| 平台与样本 | 正常浏览器实际结果 | 产品处理 |
| --- | --- | --- |
| 小红书 [新闻晨报作者页](https://www.xiaohongshu.com/user/profile/5c00a062000000000501b916) | 日常浏览器提示“电脑设备登录超限，请重新登录”；12 张公开卡片有标题，但笔记 ID 为空、链接只有作者路径。独立资料目录亦需要登录。 | `browser_login_required`；不使用标题伪造 ID，不把作者主页误当成笔记。用户须在专用 Chrome 登录后重新扫描。 |
| 抖音 [中国网财经作者页](https://www.douyin.com/user/MS4wLjABAAAAq3kXlON6-IM-t8_aV2Rb6LHqdg1uCL5RF3LnZiszu2g) | 作者介绍正常；作品容器显示“服务异常，重新刷新拉取数据”，没有该作者的视频卡片。日常、独立资料目录均观察到该状态。页脚存在其他视频推荐，不能当作该作者更新。 | `browser_source_unavailable`；只解析作者作品容器，排除页脚推荐，不记录成功的空扫描。当前未证明此作者的作品列表可稳定获取。 |
| 独立 Chrome 传输 | 成功正常打开 YouTube 公开视频并读取对应标题。 | 证明专用资料目录的启动、CDP 握手、页面导航及自己标签页清理可工作；不等于所有站点已完成登录验证。 |

抖音与小红书作者页解析器仅接受平台内具有真实作品 ID 的链接，去重并保存标题、作者和稳定来源链接。仅有标题时保持 `metadata`，不提升为完整访谈，也不据此生成买入结论。需要完整原文、字幕或授权转写后才能进行充分的内容分析。

## 视频字幕与正常登录

专用窗口已经提供 [小红书](https://www.xiaohongshu.com/explore)、[抖音](https://www.douyin.com/) 和 [哔哩哔哩](https://www.bilibili.com/) 的普通登录入口，登录由用户本人完成。运行时不会代填密码、导出 Cookie 或推断用户已登录。

正常播放器加载的字幕可通过本次标签页的响应获取。YouTube 必须匹配原视频 ID；哔哩哔哩必须先由当前视频 `aid/cid` 的播放器响应列出字幕轨道，再匹配同页实际加载的字幕响应。字幕地址仅在内存中做哈希绑定，不输出或重新请求带签名地址；自动生成、自动翻译的字幕保持未人工核验标记。平台控件使用正常鼠标事件，操作范围仅限本次创建的标签页。

哔哩哔哩公开播放器响应已观察到 `need_login_subtitle=true` 且字幕列表为空；这种情况显示需要登录，不能误报为不存在字幕或采集成功。登录前的结果不构成字幕全文路径已经完成验收的证据。

## 官方开放接口边界

- 抖音官方 [`video.list` / 查询授权账号视频列表](https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/search-video/account-video-list/) 要求权限申请及对应用户授权。它能服务愿意授权的创作者，不代表用户登录自己的账号后就可查询任意被关注作者的全部视频。
- 抖音官方 [查询特定视频数据](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/video-id-convert/user-video-data/video-data) 明确限制为授权令牌对应用户上传的视频；该接口并非任意投资博主订阅接口。
- 小红书公开的 [电商开放平台](https://open.xiaohongshu.com/) 及 [API 目录](https://open.xiaohongshu.com/document/api?apiId=5721&apiNavigationId=5&gatewayId=103&gatewayVersionId=1661&id=35) 覆盖店铺授权、订单、商品、库存等。本次未发现面向普通关注者、公开获取任意作者笔记更新与全文的官方接口；不能据此宣称平台所有商业授权方案都不存在。

## RSS 替代路径

应用已有用户指定 RSS/Atom 的接入能力，可接入作者提供的订阅源，或用户已有使用权限的数据服务。需要用户提供具体源地址与使用资格；本次没有配置或付费购买第三方服务。

RSSHub 的 [小红书路由源码](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xiaohongshu/user.ts) 标注浏览器及反采集限制，并有可选 Cookie 路径；当前无 Cookie 回退甚至使用封面地址和标题作为条目标识，不能直接视为可靠全文订阅。[抖音路由源码](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/douyin/user.ts) 同样依赖页面加载，若无法获得作品列表会报错。存在 RSSHub 路由并不代表该源具有稳定性保证或内容再使用授权，因此应用不默认向不明公共实例提供用户凭据。

## 验收结论

浏览器传输、明确状态、作品和作者身份校验、正文门槛、临时音轨完整性与离线测试已经落地。2026-10-02 已验证登录后两平台真实作者更新；图文、完整录音、机器转写是分别核验的步骤。平台仍可能要求重新登录、验证码，或者没有可辨识语音，不能承诺任意视频都能取得充分全文。
