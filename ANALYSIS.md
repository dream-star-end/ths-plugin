# 同花顺 Linux 客户端逆向分析（节选）

目标：同花顺 Linux 版 `cn.com.10jqka 2.9.1.0`（/opt/apps/cn.com.10jqka/files/）
方法：REA（dream-star-end/rea，rea-agents 6.4.0）+ 字符串/元数据分析 + 运行时交叉验证
日期：2026-10-12

## 1. 客户端架构

- .NET 5 自包含应用：Avalonia UI + CEF（Chromium）内嵌页 + GMS 行情 SDK
- 关键程序集：
  - `Hevo.Api.Quotes.dll`（1.08 MB）行情 API：GMS 会话、QuoteV2/V3/V4、Tick、Sort、板块
  - `Hevo.Api.Web.dll` Web 接口；`Hevo.Sdk.dll` 含问财 NLP 接口
  - `CSharpGMS.dll` GMS 协议封装（`GMS Create Session`、心跳、Passport64 认证）
  - `ThsDomainResolver.dll` / `DNSResolve.dll` 行情服务器域名解析（servers_groups_v3）

## 2. REA 使用记录

- `rea inspect-managed-artifact Hevo.Api.Quotes.dll` 成功：
  PE32 / IL-only，SHA256 `775edccb3761ca43d60c1cc65897a3fe101bd5dc3144f4db12a8060e22b83d0d`
  （证据文件 rea_out/quotes_artifact.json）
- `rea inspect-managed-members` 未能给出成员清单：REA 报告
  `resource-limit: Managed metadata exceeds the 67108864 byte representation budget`，
  覆盖状态 unavailable（rea_out/quotes_members.json）。成员级结论改用 strings/元数据与运行时验证补齐。

## 3. 实时行情通道（未复刻）

- 客户端实时行情走私有 **GMS 协议**：先经 `auth.10jqka.com.cn/verify` 取得
  AuthPassport，再连接行情服务器组（servers_groups_v3），数据为 QuoteV2/V3/V4
  二进制包（protobuf 风格），含心跳与压缩。完整复刻需持续抓包与协议还原，
  不在 v1 范围内；且涉及账号认证，不适合做无状态 CLI。

## 4. 客户端内置的 HTTP 通道（v1 已采用）

来自程序集字符串（UTF-16 #US 堆）与 Appsettings.xml：

| 通道 | 地址 | 状态 |
|---|---|---|
| 问财 NLP 解析/选股 | `https://xfer.10jqka.com.cn/webrpc_v2/nlp_v2_dataquery?query=<自然语言>&domain=abs_` | ✅ 实测：返回解析片段 + datas（代码.市场，如 600519.17；17=上交所 33=深交所） |
| 自选股 | `http://selfstock.10jqka.com.cn/my_stock.php` | ⚠️ 未登录返回加密串，需账号会话，v1 用本地自选替代 |
| 财经资讯 RSS | `https://news.10jqka.com.cn/cjzx_list/index.xml` 等（cjzx/hk/hgt/whfx/gjcj） | ✅ 实测（GBK 编码） |
| F10 | `https://basic.10jqka.com.cn/<code>/company.html`、`finance.html` | ✅ 实测；finance 页内嵌 chartData JSON（财务诊断评分/点评） |
| 登录 | upass.10jqka.com.cn 短信登录 + captcha.10jqka.com.cn 验证码 | 需人工交互，CLI 不做 |

## 5. Web 行情网关（v1 行情源，与客户端同厂商同口径）

`https://d.10jqka.com.cn/v6/`（JSONP）：

- `realhead/hs_<code>/last.js` 行情快照（字段号字典）
- `line/hs_<code>/<period>/last<N>.js` K 线：01 日 / 11 周 / 21 月 / 41 60分 / 51 30分 / 61 15分 / 71 5分
- `time/hs_<code>/last.js` 分时：`时间,现价,该分钟成交额,累计均价,该分钟成交量`

realhead 字段映射用 K 线末日数据逐项交叉验证（600519、000001 两只样本，开/高/低/收/量/额
与 K 线完全吻合，涨停=昨收×1.1、跌停=昨收×0.9 亦吻合）：

| 字段号 | 含义 | 字段号 | 含义 |
|---|---|---|---|
| 5 / name / marketid | 代码 / 名称 / 市场(17沪 33深) | 13 | 成交量(股) |
| 6 | 昨收 | 19 | 成交额(元) |
| 7 | 今开 | 264648 | 涨跌额 |
| 8 | 最高 | 199112 | 涨跌幅(%) |
| 9 | 最低 | 1968584 | 换手率(%) |
| 10 | 最新价 | 526792 | 振幅(%) |
| 69 / 70 | 涨停价 / 跌停价 | 402 / 407 | 总股本 / 流通股本 |
| 3475914 / 3541450 | 总市值 / 流通市值 | stockStatus / updateTime | 状态 / 更新时间 |

## 6. 结论

- 能稳定、无状态地提供给 agent 的能力：行情快照、K 线、分时、自然语言选股/搜索、
  资讯、F10 摘要——v1 CLI 已覆盖。
- 明确不做：GMS 协议复刻、交易下单（Linux 版客户端本身无交易功能）。
- 账号登录/自选云同步：v1 时未做，用户 2026-10-12 决定补上，见下节。

## 7. 追加逆向（2026-10-12）：账号登录 + 云自选

方法升级：REA 成员检查在小体量程序集（Normandy.Identity.Client.dll 50KB）上可用；
再用 ILSpy（ilspycmd 8.2 + 自装 .NET SDK 8）反编译 Normandy.Identity.Client、
HevoNext.UI.Avalonia（VerificationSlider）、HevoNext.B2CApp（LoginViewModel）、
Hevo.SelfStock、Hevo.Core（HevoHelper），反编译产物在 `ths_cli/decompiled/`。

### 短信登录协议（upass）

1. **RSA 公钥**：GET `auth.10jqka.com.cn/verify2?reqtype=do_rsa&type=get_pubkey`
   → XML：rsa_version=default_5、modulus（hex）、publicExponent=10001（1024 位）。
   字段加密 = GBK 字节 → RSA PKCS#1 v1.5 → **标准 Base64** → URL 编码（HevoHelper.HexinBase64Encode 实为标准 Base64，经实机调用客户端 DLL 验证；自定义字母表只用于自选接口）。
2. **滑块验证码**（captcha_type=4，appid=pc_yhb，宿主 captcha.10jqka.com.cn）：
   - GET `/captcha/v2/getprehandle?appid=pc_yhb&captcha_type=4` → JSON
     {sign, urlParams, imgs:[背景id, 拼图id], initx, inity}
   - GET `/getImg?{urlParams}&iuk={id}`：背景是 JPEG（340×195，带白色拼图缺口），
     拼图是 PNG（42×55）
   - 用户拖动后 phrase = `"{left};{top};{背景宽};{背景高}"`（背景图像素坐标）
   - GET `/getTicket?{urlParams}&phrase={phrase}` → {code:"0", ticket}
   - signature 即 prehandle 的 sign。无滑块直接发短信会被拒：
     errorcode -11400「请先完成滑块验证码校验」（已实测）
3. **发短信**：GET `upass.10jqka.com.cn/smslogin/sendSms?` + 字典序参数
   （captcha_phrase/signature/ticket/type=4、country_code=86、mobile=加密手机号、
   request_type=login、rsa_version、source=pc_yhb）→ {errorcode, errormsg}
4. **验短信**：POST `upass.10jqka.com.cn/smslogin/checkSms`（GBK 表单，参数同上
   + longLogin=1、signcode=加密验证码）→ JSON
   **{errorcode:0, account, passwd, expireTime}**——直接下发账号凭据
   （LoginViewModel.Login 的解析逻辑为据；实测字段名是 passwd 不是 password，
   expireTime 是 unix 时间戳，约 30 天）。
   之后的 unified_login 会话（verify2）只服务于 GMS Passport，CLI 不需要。
5. 密码登录走 /verify3 salt 协议（Appid=2022021120720687、SymmetricKey=
   "thsqhtPC88^"、X-Auth-Sign=HmacSHA256，见 Normandy 反编译），CLI 未实现，
   短信凭据已满足云自选需求。

### 同花顺自定义 Base64（HX）

字母表（由 HevoHelper 解码表反推，编解码均与服务器实测互通）：
`oPbsG4EvU8gyd02B3q6fIVWXYZaCcMeTKhxnwzmjApRrDtuHkiLlN1O9F5S7JQ+/`
验证：匿名请求自选接口的响应 blob 解码得「<meta &ReturnMsg=用户或密码错误.&retcode=-3」。

### 云自选协议（selfstock）

- POST `selfstock.10jqka.com.cn/my_stock.php?`，
  body = `param=` + HXBase64(GBK(明文 query))；响应同法编码，解码后为
  `retcode=0&ReturnMsg=...&num=N&Version=...&Rtime=...&SelfStock=<代码|...>,<市场OldID|...>`
- 拉取：`do=get&account=<account>&passwd=<password>&selfcode_crc= &marketcode=1&expand=1`
- 推送：`do=put&account=..&passwd=..&selfstock=<代码,...>&selfcode_crc= `
  （SelfStockAuthDto 的 UserName/PassWord 即 UserToken 的 Account/InputPassword，
  与 checkSms 下发的 account/password 对应——B2CApp LoginSuccess 代码为据）
- 市场 OldID 映射未知全表：CLI 用 sync 拉回的 (code, old_id) 对自学习
  （按代码推市场再套同市场 old_id），push 时缺映射会明确报错。

## 8. 追加（2026-10-12）：深度 F10 + 板块/排行

- **财报三表**：`basic.10jqka.com.cn/api/stock/finance/{code}_debt|_benefit|_cash.json`
  外层 JSON 的 flashData 为内嵌 JSON 字符串：title=科目行（`*` 前缀为核心指标），
  report[0]=报告期、report[i]=各期数值（API 已格式化为"460.33亿"等字符串）。
- **十大股东**：`basic.10jqka.com.cn/{code}/holder.html` 服务端渲染；
  锚点"前十大流通股东累计持有"/"前十大股东累计持有"后取表格行（股东名在 `<th>`，
  其余在 `<td>`），最新一期。
- **板块指数行情**：v6 网关支持 `bk_{881xxx/885xxx}` 符号（realhead 字段口径同个股）；
  板块名来自 `v6/time/bk_{code}` 的 name 字段。注册表构建：枚举行业
  881100–881499 + 概念 885000–886199 共 1600 个代码并发验证，得 95 行业 + 431 概念，
  缓存于 ths_cli/sectors.json（`sector list --refresh` 重扫，约 90 秒）。
- **板块成分股 / 全市场排行**：q.10jqka.com.cn 与 data.10jqka.com.cn 有 chameleon
  反爬（401 JS 挑战），改走问财 NLP：`{板块名}板块的股票`（半导体实测 184 只）、
  排行模板（"今日涨幅前N的股票"/"成交额最大的N只股票"/"换手率最高的N只股票"等，
  措辞经实测调校，NLP 对说法敏感）+ 并发补行情后本地重排。属近似榜单，输出带 note。
- **资金流/龙虎榜**：数值明细未找到可匿名直连的干净数据源（data.10jqka 被 chameleon
  墙：主页可开、ajax 接口 401，须执行其 JS 计算 token，脚本版本滚动更新）；
  名单版已补：`rank --by fund|fundout`（问财主力资金流向名次，无金额）、
  `lhb`（问财龙虎榜名单，无营业部明细）。不拿未验证字段凑数。
- **盘口**：realhead 字段 24/25=买一价/量、30/31=卖一价/量（股），已用茅台/
  平安银行/拓维信息验证 bid<=price<=ask，quote 输出 order_book（含委比）。
  五档其余档位网关不稳定下发；逐笔/十档属客户端 GMS + Level-2，未复刻。

### CLI 实现状态

- 已真机联调通过（2026-10-12）：滑块定位（网格读图法：给背景图叠 10px 网格，
  x ≈ 缺口主体左缘 − 4，y 固定为 inity，容差约 ±5）→ 发短信 → verify →
  sync 拉到云自选 18 只（含指数/ETF）。push 已于 2026-10-12 实测通过（同名单推送 retcode=0「修改自选股成功」，推送后 sync 复核云端仍为 18 只一致）；实测中发现并修复：指数代码 1A0001 使 normalize 失败导致 push 中断（改为快照 old_id 优先 + 静默试探）。
- **2026-10-12 agent 向优化**：新增 indicators（MA/MACD/RSI/KDJ/BOLL 本地计算）、doctor（通道健康检查，实测当场抓到网关 502 瞬时故障）、commands（能力清单）；K 线标定为前复权并在输出标注 adjust=forward（比亚迪 2025-07-29 送转除权日前后价格连续验证）；f10 --deep 补同比 yoy_pct 与 --all 全量科目；http 层对 GET 增加 502/503/网络错误重试（POST 不重试防重复提交）；watchlist list 改并发（18 只约 20s→3.4s）；quote 支持指数代码直查（1A0001）。
  经验：短信验证码有效期很短（约 3–5 分钟，首个码 4 分钟即过期），拿到立刻 verify；
  登录字段加密用标准 Base64（HexinBase64Encode 经实机调用客户端 DLL 验证），
  自定义字母表仅自选接口使用，两者勿混。
- 凭据存 `~/.ths_auth.json`（0600）；登录流程状态存 `~/.ths_login_flow.json`（0600，
  含滑块 ticket 与公钥，verify 后删除）。
