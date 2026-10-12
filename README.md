# ths-plugin · 同花顺 A 股行情 Agent 插件

给 AI Agent 用的同花顺行情工具包：**一个零依赖 CLI + 一份 Agent Skill**。
Agent 读 `SKILL.md` 学会调用 `ths` 命令，即可查询 A 股行情、K 线、选股、
深度 F10、板块、排行、龙虎榜，并可短信登录同花顺账号同步云自选股。

数据源为同花顺公开 Web 接口（逆向自同花顺 Linux 客户端，方法见
[ANALYSIS.md](ANALYSIS.md)）。**无交易功能。**

## 功能一览

| 命令 | 说明 |
|---|---|
| `ths quote <代码...>` | 实时行情快照（现价/涨跌幅/量额/换手/涨跌停/市值 + 买一/卖一盘口与委比） |
| `ths kline <代码> --period day\|week\|month\|60m\|30m\|15m\|5m` | K 线（前复权） |
| `ths indicators <代码>` | 技术指标 MA/MACD/RSI/KDJ/BOLL（本地计算） |
| `ths doctor` / `ths commands` | 通道健康检查 / 机器可读能力清单 |
| `ths events <代码>` | F10 公司大事·近期重要事件（披露/股东大会/业绩/回购/分红/解禁） |
| `ths intraday <代码>` | 当日分时（1 分钟） |
| `ths search <关键词>` | 按名称/代码找股 |
| `ths screen "<自然语言>"` | 问财 NLP 选股，如「市盈率低于 10 的银行股」 |
| `ths news --source cjzx\|hk\|hgt\|whfx\|global` | 财经资讯（RSS） |
| `ths f10 <代码> [--deep] [--all]` | F10 摘要；`--deep` 附财报三表核心指标（近 4 期，含同比）+ 十大股东，`--all` 附全量科目 |
| `ths rank --by change\|fall\|amount\|turnover\|volume\|fund\|fundout` | 全市场排行（资金流为名单版，无金额） |
| `ths lhb` | 龙虎榜名单（最近交易日，无营业部明细） |
| `ths sector list\|quote\|stocks` | 板块：95 行业 + 431 概念（注册表 `sectors.json` 随仓库附带） |
| `ths watchlist list\|add\|remove` / `sync` / `push` | 本地自选 / 云自选同步（需登录） |
| `ths login send-code\|verify\|status\|logout` | 短信验证码登录（滑块验证码由 Agent 看图定位） |

所有命令默认 JSON 输出；全局选项（任意位置）：`--table` 表格、`--compact` 单行 JSON、`--fields a,b` 字段裁剪。带短期文件缓存（行情 20s、K 线 2–30 分钟、财报 6h，`THS_NO_CACHE=1` 禁用），错误输出带 `code` 字段便于程序分支。K 线/分时/指标的目标可为股票代码、指数代码、板块代码或板块名；北交所 920xxx 已实测可查。单元测试：`python3 -m unittest discover -s tests`。

## 安装

```bash
git clone https://github.com/dream-star-end/ths-plugin.git
cd ths-plugin
./ths quote 600519        # 直接可用：仅需 Python 3.8+，无第三方依赖
```

可选：软链到 PATH：

```bash
ln -s "$PWD/ths" ~/.local/bin/ths
```

### 作为 Agent Skill 安装

把本仓库目录（或其中的 `SKILL.md` + `ths` + `ths.py` + `sectors.json`）
放进你的 Agent 的 skills 目录即可，例如 Muse：

```bash
cp -r ths-plugin ~/.muse/skills/ths-cli   # 目录名可自定
```

Skill 会教 Agent 何时调用哪个命令、如何处理登录验证码流程，
以及哪些数据拿不到（避免 Agent 编造）。

## 登录说明

`ths login` 走同花顺短信登录：滑块验证码图片保存在 `~/.ths_captcha/`，
由 Agent 看图给出缺口 x 坐标；短信码由用户提供（有效期约 3–5 分钟）。
凭据只存本机 `~/.ths_auth.json`（权限 0600，约 30 天有效），不上传任何服务器
（同花顺官方接口除外）。

## 已知边界

- 行情为 Web 网关快照轮询（交易时段近实时），非逐笔推送；
  完整五档/逐笔属客户端 GMS 协议 + Level-2 付费数据，未复刻（`quote` 只带买一/卖一）。
- 个股资金流金额、龙虎榜营业部明细位于 data.10jqka.com.cn 的 JS 反爬墙后，
  仅提供问财名单版（`rank --by fund`、`lhb`），无金额/明细数值。
- 排行与板块成分股经问财 NLP 解析 + 行情重排，属近似榜单，输出自带来源说明。
- 非交易时段返回最近收盘数据（输出含 `status` / `update_time`）。

## 免责声明

本项目为个人学习研究作品，与同花顺官方无关；数据版权归同花順及相关
交易所所有。接口为未公开 Web 接口，可能随时变更或失效。
使用者应遵守相关服务条款，勿用于商业或高频抓取用途。

## License

MIT
