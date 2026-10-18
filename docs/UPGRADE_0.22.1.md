# 0.22.1：按当前研究相关性组织新闻来源

用户于 2026-10-18 明确要求根据当前标的、消息时效和相关性优化来源，并核实是否值得补充 X 人物渠道。本次实现限于公开资料采集与候选排序；没有修改研究提示词、深研证据门槛、交易资格、风险限制或模型调用预算。

## 来源选择与依据

近七天的本地程序统计中，新浪、Bloomberg、CNBC、FT、BBC 分别产出 66、38、19、17、9 条 DEEP 筛选结果；这说明它们有发现价值，不代表交易有效性或收益。WHO 新入库 90 条均为发现时已经超过七天的历史资料，未产生深研事件。稀疏的一手来源仍保留，不因一周发布少而判为无用。完整统计只留本机代理笔记，不上传原始资料。

新增五个免费公开官方订阅（配置来源由 19 增至 24）：

| 来源 | 用途 | 官方入口依据 |
|---|---|---|
| 中国人民银行 | 国内货币、信贷、金融政策 | [官网](https://www.pbc.gov.cn/)、[新闻 RSS](https://www.pbc.gov.cn/goutongjiaoliu/113456/2986536/index.html) |
| NVIDIA 正式发布稿 | 算力供给、数据中心、芯片与机器人产业线索 | [RSS 目录](https://nvidianews.nvidia.com/rss)、[发布稿订阅](https://nvidianews.nvidia.com/cats/press_release.xml) |
| OpenAI 官方新闻 | 算力投资、基础设施和有产业影响的新发布 | [官方新闻及 RSS](https://openai.com/news/) |
| SEC 监管公告 | 市场准入、加密资产监管与重要执法 | [官方 RSS 说明](https://www.sec.gov/about/rss-feeds) |
| Ethereum Foundation 英文博客 | 以太坊协议升级、安全与生态证据 | [官方 RSS 说明](https://blog.ethereum.org/2022/08/31/blog-internationalization) |

NVIDIA 使用正式发布稿分类订阅，避免普通订阅混入博客营销内容；按其[个人非商业信息用途要求](https://www.nvidia.com/en-us/about-nvidia/rss/)使用，不扩大原文再分发。Ethereum 源站经常只提供日期，午夜时间不解释为精确事件时刻。两个大订阅仍受每次最多解析 100 条的限制，不宣称完整历史覆盖。人行相对文章链接先按该源固定地址补全，再检查 HTTPS 与严格同站域名。

## 本地排序与轮询

候选读取现有研究名单中的公司和固定资产，以公开名称、安全代码边界及业务关键词做本地相关性排序。公开词典仅作选材提示，不证明受益关系，也不改变观察或交易资格。修复了“无动态 FOCUS 时，CORE 公司新闻得不到相关性加分，甚至被无宏观主题过滤”的缺口。保留新发现、全球冲击及来源多样性的空间，相关性分数不进入模型的新闻提示词。

主要财经与政策来源继续随约 30 分钟新闻周期检查；EIA 为 60 分钟、联合国为 120 分钟、WHO 为 360 分钟。新来源不等于更低的端到端延迟，读取至研究反馈仍受现有调度和模型忙碌情况影响。新闻时间与实际首次发现时间保持分开，旧稿不会因为重抓变成新稿。

失败来源按 1、2、4、6 小时退避（不短于其正常间隔）；跳过检查不更新最后成功时间，不假报健康。覆盖页显示检查间隔、下次检查及真实失败/陈旧状态。BLS 官方新闻接口仍拒绝访问，继续标注缺口；其时序数据 API 不冒充新闻直连。

ECB 原 URL 正常，失败原因是本机 Python 信任根不足。仅在 macOS 发生证书链验证错误时，尝试用固定系统根证书库中的公开 CA 补充默认验证上下文，然后按原域名限制重试一次。没有安装证书、读取用户钥匙串或关闭证书/主机名验证；无法安全恢复则保留原失败。

## X 候选，不计入已接通覆盖

优先核实的账号为机构 `@NVIDIA`、`@OpenAI`、`@ethereum`、`@SECGov`、`@federalreserve`、`@US_FDA`，及人物 `@elonmusk`、`@sama`。仅关注有具体新增政策、供需、资本开支、协议安全或部署事实的内容；不因人物知名度自动深研。黄仁勋个人账号不纳入未经核实的名单，采用公司正式渠道。

身份依据：[NVIDIA 正式披露](https://investor.nvidia.com/files/doc_financials/2026/q3/13e6981b-95ed-4aac-a602-ebc5865d0590.pdf)、[OpenAI 官网](https://openai.com/)、[Ethereum 官方文章](https://blog.ethereum.org/2026/01/20/checkpoint-8)、[SEC 目录](https://www.sec.gov/opa/socialmedia)、[Fed 官方链接](https://www.federalreserve.gov/website-linking-policies.htm)、[FDA 官方说明](https://www.fda.gov/consumers/consumer-updates/fda-pharmacists-help-you-use-medicines-safely)、[Tesla 正式披露](https://ir.tesla.com/_flysystem/s3/sec/000156459022011875/tsla-8k_20220328-gen.pdf)、[Altman 本人博客](https://blog.samaltman.com/a-question)。

[X 官方实时流](https://docs.x.com/x-api/posts/filtered-stream/introduction)需要开发者凭据，采用[按量付费](https://docs.x.com/x-api/getting-started/pricing)。本轮没有注册、购买、保存令牌或接入 X，不宣称监控原帖或秒级反馈。已有官方 RSS 的机构，也不能假定其社交账号比官网更快。

## 验证与回退

覆盖解析与来源域名、原始发布时间、失败退避/恢复、低频健康状态、公司和代码匹配、当前名单、探索名额、模型输入边界以及 TLS 系统根证书的单元测试；上线前完整 Python 和网页测试必须通过。公开来源另以生产 Python 实测，不把网络实时请求放进单元测试。

失败标准：新源不能严格校验或正确解析时间；当前公司候选失去优先权；全球突发被公司新闻挤出；跳过检查伪报成功；研究或交易门槛被意外改变。出现上述情况应回退本次代码，不改数据库历史。新增源及排序只产生后续观察证据，没有用这次历史统计证明收益改善。
