# Changelog

## 1.3.0（2026-10-12）

面向 AI agent 使用的系统性优化：

- 新增 `indicators`（MA/MACD/RSI/KDJ/BOLL 本地计算）、`doctor`（通道健康检查）、
  `commands`（机器可读能力清单）、`events`（F10 公司大事·近期重要事件）
- K 线标定为**前复权**（比亚迪送转除权日验证），输出标注 `adjust=forward`
- `kline`/`intraday`/`indicators` 支持板块目标（板块名/881xxx/885xxx 代码/bk_ 前缀），
  `quote` 支持板块代码与指数代码（1A0001）直查
- 北交所支持：920xxx 代码行情/F10 均可查（网关 hs_ 前缀）
- `f10 --deep` 核心指标补同比 `yoy_pct`，新增 `--all` 全量科目
- 输出瘦身：全局 `--compact`（单行 JSON）、`--fields`（字段裁剪）
- 错误带 `code` 字段（INVALID_CODE/NOT_FOUND/UPSTREAM/NETWORK/RATE_LIMIT 等）
- 短期文件缓存（行情 20s、K 线 2–30min、财报 6h；`THS_NO_CACHE=1` 禁用）
- GET 请求对 502/503/网络错误自动重试（POST 不重试）
- `watchlist list`/`screen` 补全改并发；`watchlist push` 实测通过
  并修复指数代码导致的中断

## 1.0.0（2026-10-12）

首个公开版本：quote/kline/intraday/search/screen/news/f10/rank/lhb/sector/
watchlist/login（短信登录 + 云自选同步），逆向自同花顺 Linux 客户端 Web 接口。
