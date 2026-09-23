# EMT 仿真接入与重试

更新：2026-09-18。**北京时间 10:31，普通仿真账户登录、资金/持仓/委托/成交四项查询及登出全部通过。此前端口连接被拒绝的现象已恢复；工作台尚未同步该账户，券商下单仍未接入。**

## 本次实际结果

| 项目 | 结果 |
| --- | --- |
| Mac / Docker | Apple Silicon；Docker Desktop 4.91.0；已开启 Rosetta |
| 运行环境 | Linux amd64，Python 3.10.21，镜像 `dean-emt-query:2.27.0` |
| EMT SDK | 官方 2.27.0 包，运行时返回 2.27.0.0；加载、初始化、版本查询通过 |
| 普通仿真账户登录 | `login_verified=true`；账户类型 0（普通现金账户），交易日 20260918 |
| 本次端口检查 | 同一地址 `61.152.230.41:19088`；Mac 306ms、Docker 339ms，均连接成功；主机路由仍为 VPN `utun0` |
| 资金、持仓、委托、成交 | 全部收到完整回调，`status=QUERY_VERIFIED`；资产 1 条、持仓 44 条、委托 0 条、成交 0 条 |
| 正常登出 | `logout_verified=true`；一次性容器及临时凭据已清理 |
| 交易 | 没有发送买卖、撤单或转账指令 |

此前的十进制 1008 表示 TCP 未建立，错误码来自 SDK 内的 `doc/EMTTraderApi错误编码列表.html`；不要混同于十六进制 `0x1008`（十进制 4104，clientID 重复）。本次复用了用户申请资料中的普通仿真凭据，以及原地址、Docker 镜像和查询代码，实际登录及查询成功；无需因此重装 Docker 或修改账号密码。两次白天网络检查均成功，但不能据此承诺端口持续可用，也不能把此前失败直接归因于夜间维护或 VPN。下方保留历史故障证据。

结果独立保存在 `/Users/dean/Documents/Codex/Agents/ashare-data/broker/emt-simulation/`：

- `sdk-check.json`：运行环境与 SDK 初始化结果。
- `latest-query.json`：最后一次登录/查询结果。
- `query-recheck-20260918-103145.json`：本次完整成功查询，独立保留。
- `network-recheck-20260918-1024.json`：本次首次成功的两侧网络检查。
- `network-check.json`、`network-direct-20260918-001259.json`：历史失败诊断；旧查询亦单独归档。

这是券商提供的仿真资产和持仓，不是项目的 10 万元策略账本或真实证券资产。结果没有进入研究资料库或研究模型输入，也没有覆盖网页的独立模拟账户。此次没有新增自动登录或定时查询任务。

## 历史：9 月 18 日凌晨断开 VPN 后仍连接被拒绝

已读取用户保存的完整结果：北京时间 **00:12:58—00:12:59**，主机路由为 Wi-Fi `en0`、`tunnel_detected=false`。Mac 直连返回 `ECONNREFUSED`（errno 61，36ms），Docker 返回同类错误（Linux errno 111，198ms）。原始结果已单独保存到 `network-direct-20260918-001259.json`，不会被后续复测覆盖。

因此，不能把当时失败仅归因于 VPN，Docker / SDK 也不是这一网络错误的必要条件。截图、脚本、官方 SDK 示例中的地址均为 `61.152.230.41:19088`。单凭这些拒绝连接记录无法判断来源是服务端还是沿途网络；当时账号鉴权尚未完成，没有证据表明账号密码错误。白天复测已恢复并完成鉴权，最新结论以上方结果为准。

若需追查历史故障，可参考 [EMT 连接问题反馈](EMT_CONNECTION_DIAGNOSIS.md)，其中已补充恢复时间。以下保留前一次 VPN 排查经过。

## 历史：9 月 17 日诊断发现 VPN 路由（23:23 起复测）

用户 23:22 的登录结果仍为 1008，SDK 初始化通过。复测 Mac / Docker 的目标端口均被拒绝，而 EMT 官网 HTTPS 可达。读取目标路由发现流量经过 `utun0`，网关 `10.8.0.1`，本机 OpenVPN Connect 正在运行。Mac 与 Docker 可能共用这一 VPN 出口，不能把它们算作两条独立网络来排除本机网络问题。

只将一次不含凭据的测试连接绑定到 Wi-Fi `en0`，系统立即返回网络不可达；当前 VPN 路由下未能完成有效的直连对照。**VPN 是需要验证的线索，尚未证明它是故障根因。** 未关闭 VPN、改系统路由或修改代理配置。

[EMT 官网](https://emt.eastmoneysec.com/) 标注 7×24 小时测试环境，因此不能仅因夜间就推断测试服务关闭。当时建议临时断开 VPN 对照；该对照现已完成且仍失败，结论以上方 9 月 18 日更新为准。

## 你如何重试

保持 Docker Desktop 运行。镜像已经构建好，通常无需重复安装或构建。

先检查运行环境，不需要账号且容器断网：

```sh
cd /Users/dean/Documents/Codex/Agents/ashare-agent
.venv/bin/python tools/emt_probe.py --check
```

先检查网络（不需要账号密码，也不发起登录）：

```sh
.venv/bin/python tools/emt_probe.py --network-check
```

`NETWORK_READY` 仅表示 Docker 能建立 TCP 连接，不代表账号已登录；`NETWORK_BLOCKED` 会显示两侧测试结果及检测到的 VPN 路由。当前配置已实测可用，不需要例行切换 VPN；只有再次出现连接问题时才按诊断结果作网络对照。网络检查不使用证券凭据，也不修改系统网络设置。

网络通过后执行查询：

```sh
cd /Users/dean/Documents/Codex/Agents/ashare-agent
.venv/bin/python tools/emt_probe.py --output /Users/dean/Documents/Codex/Agents/ashare-data/broker/emt-simulation/latest-query.json
```

脚本先检查网络，只有 Docker 能连接目标端口后，终端才依次提示“普通仿真账号”和“仿真密码”，填入 EMT 申请记录中的**普通**账号及初始密码，输入不会显示。这里不需要 OpenAI API Key，也不使用真实证券账户密码；不用写入 `config.json` 或 `.env`。

成功标准是 `status: QUERY_VERIFIED`、`login_verified: true`、`queries_complete: true`。查询必须收到对应请求、对应会话的完整结束回调；失败、超时或断线不能当成零持仓。脚本固定使用本次仿真地址，校验普通现金账户，clientID 为 97。

若断开 VPN 或切换网络后仍是 1008，可向 EMT 支持反馈：“普通仿真 API 申请已完成，SDK 2.27.0 初始化正常，Mac 与 Docker 直连申请页地址均被拒绝，登录返回 1008。请确认测试地址、服务时段和当前运行状态。”不要附上密码。

仅当镜像被删除或迁移到新机器，重新构建：

```sh
.venv/bin/python tools/emt_probe.py --build
```

构建脚本使用固定摘要的 Python 基础镜像，下载并校验 Debian `lshw` 依赖；官方 SDK 不复制进镜像，运行时核验压缩包及动态库后只读挂载。构建需要联网，但不购买服务。应使用此命令构建，脚本会准备 Dockerfile 所需的依赖包。

## 实现与边界

- 工具 `tools/emt_probe.py`；镜像定义 `docker/emt-query/Dockerfile`；测试 `tests/test_emt_probe.py`。
- SDK 日志路径必须传入 `/work/`，让拼接后的日志留在可写临时目录。传入 `/work` 会尝试写到只读根目录下。
- 官方 Python 包的 `exit()` / 对象析构在本机 Docker 中可复现阻塞，无账号、断网时也会发生。本工具采用一次性进程：查询后尝试正常登出，输出 `logout_verified`，再在保留 SDK 对象引用时退出进程；主机移除容器及临时文件。这不是已验证的常驻交易连接。
- 凭据只经标准输入传入容器；未放入 Docker 参数、环境变量、镜像、项目文档或研究资料。原始 SDK 输出被屏蔽，容器禁用持久日志和 core dump；本次临时凭据文件已删除，后续可在终端重新输入。
- SDK 和脚本暂存于 `/private/tmp`，避免 Docker 访问 Documents 时阻塞；仅挂载这两项，不挂载家目录或 Docker socket。容器不开放端口、根文件系统只读、工作目录使用临时内存盘；100 秒超时后停止并移除。结果文件仅当前用户可读写。
- SDK 来源及校验值见 [SDK 清单](emt-sdk-manifest.json)，官方下载见 [EMT Python SDK](https://emt.eastmoneysec.com/down/other-language/1)。仿真申请有效期截至 2027-09-17；本工具只用普通账户。

## 后续验收

1. 已完成普通仿真登录、资金/持仓/委托/成交查询、交易日及账户类型核对、正常登出；仍需验证长期可用性与异常恢复。
2. 单独实现仿真委托、撤单、成交对账、断线恢复与重复请求防护。官方 `test/tradertest.py` 含交易调用，不能直接当作查询脚本运行。
3. 工作台接入时明确显示“EMT 仿真账户”，保留现有本地模拟账本；真实账户权限和执行能力仍需单独开通、实现和验收。

当前 `mode=paper`、`live_execution_enabled=false`。Docker 能运行 SDK，不代表真实账户或自动交易已启用。
