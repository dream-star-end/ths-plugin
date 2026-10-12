---
name: ths-cli
description: 同花顺行情 CLI：当用户问 A 股行情、K 线、分时、选股、个股资讯、F10、板块、排行、龙虎榜或自选股时，用本插件的 ths 命令获取同花顺数据（JSON 输出）。工具逆向自同花顺 Linux 客户端 Web 接口，不含交易功能。
---

# 同花顺 CLI（Agent 插件）

可执行文件：本插件目录下的 `ths`（即 SKILL.md 同级的 `ths`，纯 Python 3 标准库，零依赖）。
先定位插件目录（本文件所在目录），后续命令都用 `<插件目录>/ths` 调用；也可将其软链到 PATH。
完整说明见同目录 `README.md`；逆向依据见 `ANALYSIS.md`。

## 用法

所有命令默认输出 JSON；错误时 stderr 输出 `{"error": ...}` 且退出码 1。

- 实时行情：`ths quote 600519 000001`（现价、涨跌幅、量额、换手、涨跌停、市值）
- K 线：`ths kline 600519 --period day|week|month|60m|30m|15m|5m --count 30`（前复权，输出 adjust=forward）
- 技术指标：`ths indicators 600519 [--count 5]`（MA/MACD/RSI/KDJ/BOLL，本地计算，优先用它而不是自己拿 K 线手算）
- 分时：`ths intraday 600519`
- 按名称找股：`ths search 茅台`
- 自然语言选股：`ths screen "今日涨幅最大的股票" --limit 20`
- 资讯：`ths news --source cjzx|hk|hgt|whfx|global --limit 10`
- F10 摘要：`ths f10 600519`（公司资料 + 财务诊断评分/点评）
- 深度 F10：`ths f10 600519 --deep [--all]`（财报三表核心指标近 4 期含同比 yoy_pct + 十大流通/十大股东；--all 附全量科目）
- 自检：`ths doctor`（各通道健康检查，拿不准接口是否正常时先跑它）；`ths commands`（机器可读能力清单）
- 全市场排行：`ths rank --by change|fall|amount|turnover|volume|fund|fundout --limit 20`（问财 NLP + 行情重排的近似榜单，引用时说明来源；fund/fundout 是主力资金净流入/流出名单，只有名次无金额）
- 龙虎榜名单：`ths lhb --limit 30`（最近交易日；上榜原因/营业部明细拿不到，不要编造）
- 盘口：`ths quote` 输出含 `order_book`（买一/卖一价量 + 委比 imbalance_pct）；只有一档，五档/逐笔没有，不要假装有
- 板块：`ths sector list [--type industry|concept]`、`ths sector quote 半导体`（板块指数行情）、`ths sector stocks 半导体 --limit 30`（成分股按涨幅排序）
- 本地自选：`ths watchlist list|add|remove [代码]`
- 云自选（需登录）：`ths watchlist sync`（云→本地合并）、`ths watchlist push`（本地→云）
- 表格输出：任意位置加 `--table`

## 登录（短信验证码，需要时向用户索取信息）

三步，全程凭据只存本机 `~/.ths_auth.json`（0600），不要把 password 打印给用户：

1. 向用户要**手机号**，执行 `ths login send-code --phone <手机号>`。
   返回 `captcha_required` 后定位滑块缺口（背景图 340×195）：给 bg 图叠 10px
   网格读图最准（可用 ffmpeg/python 生成网格图）；**x ≈ 缺口主体左缘 − 4，
   y 不用传（固定为流程的 inity，拼图只能横向拖动）**，容差约 ±5。
2. `ths login send-code --phone <手机号> --captcha-x <x>`
   返回 `code_sent` 后，向用户要**短信验证码**——有效期只有约 3–5 分钟，
   让用户收到立刻发来，拿到马上执行下一步。
3. `ths login verify --code <验证码>` 完成登录；`ths login status` 查状态，
   `ths login logout` 删除凭据。凭据约 30 天有效（status 里有到期时间）。

滑块或短信失败会返回明确 error，流程自动重置，按提示重跑即可。

## 注意

- 非交易时段返回最近收盘数据，先看输出里的 `status` 和 `update_time` 再下结论。
- 代码支持 `600519`、`sh600519`、`600519.SH`、`600519.17` 等写法。
- 数据来自同花顺公开 Web 接口，未获同花顺官方授权；仅供个人学习研究，
  接口随时可能变更。没有交易下单功能；不要用它执行或承诺交易。
