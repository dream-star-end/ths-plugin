#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ths — 同花顺命令行工具（供 AI agent / 脚本直接调用）

数据源逆向自同花顺 Linux 客户端（cn.com.10jqka 2.9.1.0）：
  * search / screen : 客户端 Hevo.Sdk.dll 内置的问财 NLP 接口
                      xfer.10jqka.com.cn/webrpc_v2/nlp_v2_dataquery
  * news            : 客户端 Hevo.Api.Quotes.dll 内置的 RSS 资讯源
                      news/index/stock.10jqka.com.cn 的 *_list/index.xml
  * quote / kline / intraday : 同花顺 Web 行情网关 d.10jqka.com.cn/v6
                      （字段映射已用 K 线数据逐项交叉验证，见 ANALYSIS.md）
  * f10             : basic.10jqka.com.cn F10 页面（公司资料 / 财务指标）
  * login / 云自选   : upass 短信登录（滑块验证码 + RSA 加密字段）+
                      selfstock.10jqka.com.cn/my_stock.php（协议见 ANALYSIS.md 第 7 节）；
                      凭据只存本机 ~/.ths_auth.json（0600）

注意：Linux 客户端的实时行情走私有 GMS 协议（加密 + Passport 认证），
本工具不复刻该协议，行情走同厂商 Web 网关，字段口径与客户端一致。

全部命令默认输出 JSON；加 --table 输出人类可读表格。
仅使用 Python 标准库，无第三方依赖。
"""
import argparse
import base64
import json
import os
import random
import re
import signal
import struct
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
TIMEOUT = 20

# ---------------------------------------------------------------- http utils

def http_get(url, referer=None, encoding=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    if encoding:
        return raw.decode(encoding, errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def http_jsonp(url, referer=None):
    """同花顺 v6 网关返回 JSONP： name({...}); 提取并解析 JSON。"""
    text = http_get(url, referer=referer)
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", text, re.S)
    if not m:
        raise RuntimeError(f"网关返回格式异常: {text[:120]}")
    return json.loads(m.group(1))


def http_raw(url, data=None, ctype=None):
    """返回原始 bytes；data 不为 None 时 POST。"""
    headers = {"User-Agent": UA}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, headers=headers, data=data,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fail(msg):
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- code utils

MARKET_ID = {"17": "SH", "33": "SZ"}


def normalize_code(raw):
    """600519 / sh600519 / 600519.SH / 600519.17 -> (code, market_id|None)"""
    s = str(raw).strip().upper()
    market = None
    m = re.match(r"^(SH|SZ|BJ)?(\d{6})(?:\.(SH|SZ|BJ|17|33))?$", s)
    if not m:
        fail(f"无法识别的股票代码: {raw}")
    pre, code, suf = m.group(1), m.group(2), m.group(3)
    tag = suf or pre
    if tag in ("SH", "17"):
        market = "17"
    elif tag in ("SZ", "33"):
        market = "33"
    elif tag == "BJ":
        market = "151"  # 北交所，v6 网关同样走 hs_ 前缀时可回退
    if market is None:  # 按代码段推断
        if code.startswith(("6", "9")):
            market = "17"
        elif code.startswith(("0", "2", "3")):
            market = "33"
    return code, market


def v6_symbol(code):
    return f"hs_{code}"


# ---------------------------------------------------------------- quote

# d.10jqka.com.cn/v6/realhead 字段号 -> 含义（已与 K 线逐项交叉验证）
REALHEAD_FIELDS = {
    "10": ("price", "最新价"),
    "6": ("prev_close", "昨收"),
    "7": ("open", "今开"),
    "8": ("high", "最高"),
    "9": ("low", "最低"),
    "264648": ("change", "涨跌额"),
    "199112": ("change_pct", "涨跌幅(%)"),
    "13": ("volume", "成交量(股)"),
    "19": ("amount", "成交额(元)"),
    "1968584": ("turnover_pct", "换手率(%)"),
    "526792": ("amplitude_pct", "振幅(%)"),
    "69": ("limit_up", "涨停价"),
    "70": ("limit_down", "跌停价"),
    "402": ("total_shares", "总股本(股)"),
    "407": ("float_shares", "流通股本(股)"),
    "3475914": ("market_cap", "总市值(元)"),
    "3541450": ("float_market_cap", "流通市值(元)"),
}


def fetch_quote(code):
    symbol = v6_symbol(code)
    data = http_jsonp(
        f"https://d.10jqka.com.cn/v6/realhead/{symbol}/last.js",
        referer=f"https://stockpage.10jqka.com.cn/{code}/",
    )
    items = data.get("items", {})
    out = {
        "code": items.get("5", code),
        "name": items.get("name", ""),
        "market": MARKET_ID.get(str(items.get("marketid", "")), items.get("marketid", "")),
        "status": items.get("stockStatus", ""),
        "update_time": items.get("updateTime", ""),
    }
    for fid, (key, _label) in REALHEAD_FIELDS.items():
        v = items.get(fid)
        if v in (None, ""):
            out[key] = None
        else:
            try:
                out[key] = float(v)
            except ValueError:
                out[key] = v
    # 盘口：网关仅稳定下发买一/卖一（字段 24/25、30/31，量单位为股），
    # 已用茅台/平安银行/拓维信息交叉验证 bid<=price<=ask
    def _fv(fid):
        v = items.get(fid)
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None
    bp, bv, ap, av = _fv("24"), _fv("25"), _fv("30"), _fv("31")
    if bp is not None and ap is not None:
        book = {"bid1_price": bp, "bid1_volume": bv,
                "ask1_price": ap, "ask1_volume": av}
        if bv and av:
            book["imbalance_pct"] = round((bv - av) / (bv + av) * 100, 2)
        out["order_book"] = book
    return out, items


def cmd_quote(args):
    results = []
    for raw in args.codes:
        code, _ = normalize_code(raw)
        q, items = fetch_quote(code)
        if args.raw:
            q["raw_items"] = items
        results.append(q)
    emit(results[0] if len(results) == 1 else results, args,
         cols=["code", "name", "price", "change_pct", "open", "high", "low",
               "prev_close", "volume", "amount", "turnover_pct"])


# ---------------------------------------------------------------- kline

PERIODS = {"day": "01", "week": "11", "month": "21",
           "60m": "41", "30m": "51", "15m": "61", "5m": "71", "1m": "81"}


def fetch_kline(code, period, count):
    p = PERIODS[period]
    data = http_jsonp(
        f"https://d.10jqka.com.cn/v6/line/{v6_symbol(code)}/{p}/last{count}.js",
        referer=f"https://stockpage.10jqka.com.cn/{code}/",
    )
    rows = []
    for seg in data.get("data", "").strip().split(";"):
        if not seg:
            continue
        f = seg.split(",")
        if len(f) < 7 or not f[1]:
            continue
        rows.append({
            "date": f[0], "open": float(f[1]), "high": float(f[2]),
            "low": float(f[3]), "close": float(f[4]),
            "volume": float(f[5]), "amount": float(f[6]),
            "turnover_pct": float(f[7]) if len(f) > 7 and f[7] else None,
        })
    return {"code": code, "name": data.get("name", ""),
            "period": period, "rows": rows[-count:]}


def cmd_kline(args):
    code, _ = normalize_code(args.code)
    emit(fetch_kline(code, args.period, args.count), args)


def cmd_intraday(args):
    code, _ = normalize_code(args.code)
    data = http_jsonp(
        f"https://d.10jqka.com.cn/v6/time/{v6_symbol(code)}/last.js",
        referer=f"https://stockpage.10jqka.com.cn/{code}/",
    )
    node = data.get(v6_symbol(code), {})

    def fnum(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    points = []
    for seg in str(node.get("data", "")).strip().split(";"):
        if not seg:
            continue
        f = seg.split(",")
        # 格式（已验证）: 时间,现价,该分钟成交额,累计均价,该分钟成交量
        if len(f) >= 5 and fnum(f[1]) is not None:
            points.append({"time": f[0], "price": fnum(f[1]),
                           "amount": fnum(f[2]), "avg": fnum(f[3]),
                           "volume": fnum(f[4])})
    emit({"code": code, "name": node.get("name", ""),
          "prev_close": float(node["pre"]) if node.get("pre") else None,
          "date": node.get("date", ""), "points": points}, args)


# ---------------------------------------------------------------- search / screen (NLP)

def nlp_query(query):
    """同花顺 Linux 客户端内置的问财 NLP 接口：自然语言 -> 命中的股票代码。
    返回 (解析片段, [(code, market_id), ...])"""
    qs = urllib.parse.urlencode({"query": query, "domain": "abs_"})
    text = http_get(f"https://xfer.10jqka.com.cn/webrpc_v2/nlp_v2_dataquery?{qs}")
    data = json.loads(text)
    if data.get("code") != 0:
        fail(f"NLP 接口错误: {data.get('msg') or data}")
    body = data.get("data") or {}
    codes = []
    for item in body.get("datas", []):
        m = re.match(r"(\d{6})\.(\d+)", str(item))
        if m:
            codes.append((m.group(1), m.group(2)))
    return body.get("chunks_info", []), codes


def cmd_search(args):
    chunks, codes = nlp_query(args.keyword)
    results = []
    for code, mid in codes:
        q, _ = fetch_quote(code)
        results.append(q)
    emit({"query": args.keyword, "parsed": chunks, "results": results}, args,
         cols=["code", "name", "price", "change_pct"])


def cmd_screen(args):
    chunks, codes = nlp_query(args.condition)
    results = []
    for code, mid in codes[: args.limit]:
        try:
            q, _ = fetch_quote(code)
            results.append(q)
        except Exception as e:  # 单只失败不拖垮整批
            results.append({"code": code, "error": str(e)})
    emit({"condition": args.condition, "parsed": chunks,
          "total": len(codes), "results": results}, args,
         cols=["code", "name", "price", "change_pct", "amount"])


# ---------------------------------------------------------------- news

NEWS_SOURCES = {
    "cjzx": ("财经资讯", "https://news.10jqka.com.cn/cjzx_list/index.xml"),
    "hk": ("港股资讯", "https://stock.10jqka.com.cn/hks/hknews_list/index.xml"),
    "hgt": ("沪港通资讯", "https://stock.10jqka.com.cn/hgtxw_list/index.xml"),
    "whfx": ("外汇资讯", "https://index.10jqka.com.cn/list/whfx/index.xml"),
    "global": ("国际财经", "https://index.10jqka.com.cn/list/gjcj/index.xml"),
}


def cmd_news(args):
    label, url = NEWS_SOURCES[args.source]
    raw = http_get(url, encoding="gbk")
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        def txt(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        items.append({"title": txt("title"), "link": txt("link"),
                      "pub_date": txt("pubDate")})
        if len(items) >= args.limit:
            break
    emit({"source": label, "items": items}, args, cols=["pub_date", "title"])


# ---------------------------------------------------------------- f10

def _page_text(url):
    raw = http_get(url, encoding="gbk")
    raw = re.sub(r"<(script|style).*?</\1>", "", raw, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", txt)


def _label_dict(text):
    """把 '标签：值 标签2：值' 形式的 F10 文本切成字典。"""
    parts = re.split(r"([一-龥A-Za-z（）() ]{2,12})[：:]\s*", text)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip()
        val = parts[i + 1].strip()
        if key and val and key not in out:
            out[key] = val
    return out


def cmd_f10(args):
    code, _ = normalize_code(args.code)
    company = _page_text(f"https://basic.10jqka.com.cn/{code}/company.html")
    labels = _label_dict(company)
    profile = {}
    direct = [
        (r"公司名称[：:]\s*(.+?公司)", "company_name"),
        (r"所属地域[：:]\s*(\S+)", "region"),
        (r"英文名称[：:]\s*([A-Za-z0-9.,&' ]+?)(?=\s*[一-龥]{2,}[：:]|$)", "english_name"),
        (r"所属申万行业[：:]\s*(.+?)(?=\s*(曾\s*用\s*名|[一-龥]{2,})[：:]|$)", "industry"),
        (r"曾\s*用\s*名[：:]\s*(.+?)(?=\s*(公司网址|上市日期|主营业务)[：:]|$)", "former_name"),
        (r"主营业务[：:]\s*(.+?)(?=\s*(产品名称|经营范围|公司网址)[：:]|$)", "main_business"),
    ]
    for pat, key in direct:
        m = re.search(pat, company)
        if m and m.group(1).strip():
            profile[key] = m.group(1).strip()[:200]
    for label, key in [("公司网址", "website"), ("上市日期", "list_date")]:
        v = labels.get(label)
        if v and key not in profile:
            profile[key] = v[:200]
    # 财务诊断：finance.html 内嵌 chartData JSON（评分 + 点评，含毛利率/净利率等）
    raw = http_get(f"https://basic.10jqka.com.cn/{code}/finance.html",
                   encoding="gbk")
    diagnosis = None
    m = re.search(r'id="chartData"\s+value=\'(\[.*?\])\'', raw, re.S)
    if m:
        try:
            arr = json.loads(m.group(1))
            if arr:
                latest = arr[0]
                period = next(iter(latest.keys()))
                cats = {}
                for cat, node in latest[period].items():
                    if isinstance(node, dict) and "comment" in node:
                        cats[cat] = {"score": node.get("score"),
                                     "comment": node.get("comment"),
                                     "good": node.get("goodComment"),
                                     "worse": node.get("worseComment")}
                diagnosis = {"period": period, "categories": cats}
        except (ValueError, IndexError, KeyError):
            diagnosis = None
    q, _ = fetch_quote(code)
    payload = {"code": code, "name": q.get("name"), "profile": profile,
               "quote_snapshot": {k: q.get(k) for k in
                                  ("price", "change_pct", "market_cap",
                                   "total_shares", "turnover_pct")},
               "finance_diagnosis": diagnosis}
    if getattr(args, "deep", False):
        payload["deep"] = cmd_f10_deep(code)
    emit(payload, args)


# ---------------------------------------------------------------- deep f10 / rank / sector

def _num(v):
    if v in (None, "", "--"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return v


def fetch_statement(code, kind, periods_n=4):
    """财报三表：basic.10jqka.com.cn/api/stock/finance/{code}_{kind}.json
    外层 JSON 的 flashData 是内嵌 JSON 字符串：title=科目行，report=按期矩阵。"""
    text = http_get(f"https://basic.10jqka.com.cn/api/stock/finance/{code}_{kind}.json")
    inner = json.loads(json.loads(text)["flashData"])
    titles, report = inner["title"], inner["report"]
    periods = [p for p in report[0] if p][:periods_n]
    rows = []
    for i in range(1, len(titles)):
        t = titles[i]
        if not isinstance(t, list) or i >= len(report):
            continue
        name = str(t[0])
        vals = report[i]
        rows.append({"item": name.lstrip("*"), "core": name.startswith("*"),
                     "unit": t[1] if len(t) > 1 else "",
                     "values": {periods[j]: _num(vals[j])
                                for j in range(len(periods)) if j < len(vals)}})
    return {"periods": periods, "rows": rows}


def _td_texts(tr_html):
    """一行内的单元格文本（股东名在 <th> 里，其余在 <td>，需按序全取）。"""
    cells = []
    for td in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr_html, re.S):
        txt = re.sub(r"<[^>]+>", " ", td)
        cells.append(re.sub(r"\s+", " ", txt).strip())
    return cells


def fetch_holders(code):
    """十大股东：basic F10 holder.html 服务端渲染表格（取最新一期）。"""
    raw = http_get(f"https://basic.10jqka.com.cn/{code}/holder.html", encoding="gbk")
    out = {}
    for key, anchor in (("top10_float", "前十大流通股东累计持有"),
                        ("top10_total", "前十大股东累计持有")):
        idx = raw.find(anchor)
        if idx < 0:
            continue
        window = raw[max(0, idx - 2500):idx]
        dates = re.findall(r"20\d{2}-\d{2}-\d{2}", window)
        seg = raw[idx:idx + 40000]
        summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                         seg[:seg.find("机构或基金名称")])).strip()[:160]
        holders = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
            cells = _td_texts(tr)
            if len(cells) >= 5 and cells[0] and "名称" not in cells[0] \
                    and "累计" not in cells[0]:
                holders.append({"name": cells[0], "shares": cells[1],
                                "change": cells[2], "pct": cells[3],
                                "share_type": cells[5] if len(cells) > 5 else ""})
            if len(holders) >= 10:
                break
        out[key] = {"period": max(dates) if dates else None,
                    "summary": summary, "holders": holders}
    return out


def cmd_f10_deep(code):
    fin = {}
    for kind in ("benefit", "debt", "cash"):
        try:
            st = fetch_statement(code, kind)
            fin[kind] = {"periods": st["periods"],
                         "core_rows": [r for r in st["rows"] if r["core"]],
                         "total_rows": len(st["rows"])}
        except Exception as e:
            fin[kind] = {"error": str(e)}
    return {"financials": fin, "holders": fetch_holders(code)}


RANK_TEMPLATES = {
    "change": ("今日涨幅前{n}的股票", "change_pct", True),
    "fall": ("今日跌幅前{n}的股票", "change_pct", False),
    "amount": ("成交额最大的{n}只股票", "amount", True),
    "turnover": ("换手率最高的{n}只股票", "turnover_pct", True),
    "volume": ("成交量最大的{n}只股票", "volume", True),
    # 资金流：问财按主力资金流向排名（该通道只给名单不返回金额，且最多约 10 只）
    "fund": ("主力资金净流入最大的{n}只股票", None, None),
    "fundout": ("主力资金净流出最大的{n}只股票", None, None),
}


def enrich_quotes(code_list, max_n=None):
    """并发给一批代码补实时行情，失败的跳过。"""
    from concurrent.futures import ThreadPoolExecutor
    sel = code_list[:max_n] if max_n else code_list

    def one(c):
        try:
            q, _ = fetch_quote(c)
            return q
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=12) as ex:
        return [r for r in ex.map(one, sel) if r]


def cmd_rank(args):
    tpl, metric, desc = RANK_TEMPLATES[args.by]
    want = min(max(args.limit * 2, args.limit + 10), 100)
    query = tpl.format(n=want)
    chunks, codes = nlp_query(query)
    results = enrich_quotes([c for c, _m in codes])
    if metric is None:
        # 资金流：保持问财返回顺序（即主力资金流向名次），该通道不给金额数值
        note = ("顺序即问财主力资金流向排名（返回数量由问财决定，流出榜约 10 只）；"
                "问财通道不返回资金流金额数值，个股资金流明细在 data.10jqka 反爬墙后")
    else:
        results.sort(key=lambda r: (r.get(metric) is None,
                                    -(r.get(metric) or 0) if desc
                                    else (r.get(metric) or 0)))
        note = "问财 NLP 解析 + 实时行情本地重排，近似全市场排行（非交易所官方榜单）"
    emit({"by": args.by, "metric": metric, "query": query, "parsed": chunks,
          "note": note, "results": results[:args.limit]}, args,
         cols=["code", "name", "price", "change_pct", "amount", "turnover_pct"])


def cmd_lhb(args):
    """龙虎榜名单：问财 NLP「龙虎榜股票」（最近交易日）+ 行情补全。
    上榜原因/买卖营业部/净买入明细在 data.10jqka.com.cn 反爬墙后，此处不提供。"""
    chunks, codes = nlp_query("龙虎榜股票")
    results = enrich_quotes([c for c, _m in codes])
    results.sort(key=lambda r: -(r.get("change_pct") or 0))
    emit({"parsed": chunks, "count": len(results),
          "note": "最近交易日龙虎榜名单（问财）；上榜原因、营业部、净买入明细"
                  "在 data.10jqka 反爬墙后，未提供",
          "results": results[:args.limit]}, args,
         cols=["code", "name", "price", "change_pct", "amount", "turnover_pct"])


SECTOR_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "sectors.json")


def _block_name(code):
    data = http_jsonp(f"https://d.10jqka.com.cn/v6/time/bk_{code}/last.js")
    return data.get(f"bk_{code}", {}).get("name", "")


def scan_sectors():
    """板块注册表：枚举同花顺板块指数代码段（行业 881100-881499、
    概念 885000-886199），用 v6 time 端点的 name 字段验证并取板块名。"""
    from concurrent.futures import ThreadPoolExecutor
    candidates = [str(881100 + i) for i in range(400)] + \
                 [str(885000 + i) for i in range(1200)]
    found = {}

    def check(code):
        try:
            name = _block_name(code)
        except Exception:
            name = ""
        return code, name
    with ThreadPoolExecutor(max_workers=24) as ex:
        for code, name in ex.map(check, candidates):
            if name:
                found[code] = {"name": name,
                               "type": "industry" if code.startswith("881")
                               else "concept"}
    cache = {"scanned_at": datetime.now().isoformat(timespec="seconds"),
             "sectors": found}
    with open(SECTOR_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    return cache


def load_sectors(refresh=False):
    if not refresh and os.path.exists(SECTOR_CACHE):
        with open(SECTOR_CACHE, encoding="utf-8") as f:
            return json.load(f)
    return scan_sectors()


def resolve_sector(query, cache):
    sectors = cache["sectors"]
    q = str(query).strip()
    if re.fullmatch(r"\d{6}", q):
        info = sectors.get(q)
        return q, (info or {}).get("name", ""), (info or {}).get("type", "")
    exact = [(c, v) for c, v in sectors.items() if v["name"] == q]
    if len(exact) == 1:
        c, v = exact[0]
        return c, v["name"], v["type"]
    fuzzy = [(c, v) for c, v in sectors.items() if q in v["name"]]
    if len(fuzzy) == 1:
        c, v = fuzzy[0]
        return c, v["name"], v["type"]
    if len(fuzzy) > 1:
        fail(f"板块名不唯一，请用代码: {[(c, v['name']) for c, v in fuzzy[:10]]}")
    fail(f"板块注册表里找不到: {q}（可先 ths sector list --refresh 刷新）")


def fetch_block_quote(code):
    data = http_jsonp(f"https://d.10jqka.com.cn/v6/realhead/bk_{code}/last.js",
                      referer="https://stockpage.10jqka.com.cn/")
    items = data.get("items", {})
    out = {"code": code, "symbol": f"bk_{code}",
           "status": items.get("stockStatus", ""),
           "update_time": items.get("updateTime", "")}
    for fid, (k, _l) in REALHEAD_FIELDS.items():
        v = items.get(fid)
        out[k] = _num(v) if v not in (None, "") else None
    # 板块特有字段（成分股涨跌家数等，来自 realhead 原始字段 14/15/223-238 一带，仅透传）
    out["raw_items"] = items
    return out


def cmd_sector(args):
    if args.action in ("quote", "stocks") and not args.target:
        fail("sector quote/stocks 需要板块名或代码")
    cache = load_sectors(refresh=getattr(args, "refresh", False))
    if args.action == "list":
        items = [{"code": c, "name": v["name"], "type": v["type"]}
                 for c, v in sorted(cache["sectors"].items())
                 if args.type in ("all", v["type"])]
        emit({"scanned_at": cache.get("scanned_at"), "count": len(items),
              "sectors": items}, args, cols=["code", "name", "type"])
    elif args.action == "quote":
        code, name, typ = resolve_sector(args.target, cache)
        q = fetch_block_quote(code)
        q.update({"name": name, "type": typ})
        q.pop("raw_items", None) if not args.raw else None
        emit(q, args)
    elif args.action == "stocks":
        code, name, typ = resolve_sector(args.target, cache)
        chunks, codes = nlp_query(f"{name}板块的股票")
        results = enrich_quotes([c for c, _m in codes], max_n=150)
        results.sort(key=lambda r: -(r.get("change_pct") or 0))
        emit({"sector": {"code": code, "name": name, "type": typ},
              "total_from_nlp": len(codes), "results": results[:args.limit]},
             args, cols=["code", "name", "price", "change_pct", "amount"])


# ---------------------------------------------------------------- login / cloud
# 登录协议逆向自客户端 Normandy.Identity.Client.dll + HevoNext 的
# VerificationSlider / LoginViewModel（ILSpy 反编译，见 ANALYSIS.md）：
#   滑块验证码: captcha.10jqka.com.cn getprehandle -> getImg x2 -> getTicket
#               phrase = "缺口left;top;背景宽;背景高"
#   发短信    : GET  upass.10jqka.com.cn/smslogin/sendSms（参数字典序，
#               mobile/signcode 为 RSA(PKCS#1 v1.5, GBK) 后标准 Base64
#               Base64 字母表编码再 URL 编码；公钥来自
#               auth.10jqka.com.cn/verify2?reqtype=do_rsa&type=get_pubkey）
#   验短信    : POST upass.10jqka.com.cn/smslogin/checkSms
#               -> JSON {errorcode:0, account, password, expireTime}
#   云自选    : POST selfstock.10jqka.com.cn/my_stock.php?
#               body = "param=" + HXBase64(GBK(query))，响应用同法解码，
#               凭据就是 checkSms 返回的 account/password。
# 凭据只存本机 ~/.ths_auth.json（权限 0600），绝不输出到 stdout。

HOME = os.path.expanduser("~")
AUTH_PATH = os.path.join(HOME, ".ths_auth.json")
FLOW_PATH = os.path.join(HOME, ".ths_login_flow.json")
CLOUD_PATH = os.path.join(HOME, ".ths_cloud_watchlist.json")
CAPTCHA_DIR = os.path.join(HOME, ".ths_captcha")
UPASS = "http://upass.10jqka.com.cn"
AUTH_HOST = "http://auth.10jqka.com.cn"
CAPTCHA_BASE = "http://captcha.10jqka.com.cn"
SELFSTOCK_URL = "http://selfstock.10jqka.com.cn/my_stock.php?"

# 同花顺自定义 Base64 字母表：由 Hevo.Core HevoHelper 的解码表反推（已验证可
# 正确解码自选接口的错误响应"用户或密码错误"）。索引 62/63 为 '+' '/'。
_HX_DEC = {43: 62, 47: 63, 48: 13, 49: 53, 50: 14, 51: 16, 52: 5, 53: 57,
           54: 18, 55: 59, 56: 9, 57: 55, 65: 40, 66: 15, 67: 27, 68: 44,
           69: 6, 70: 56, 71: 4, 72: 47, 73: 20, 74: 60, 75: 32, 76: 50,
           77: 29, 78: 52, 79: 54, 80: 1, 81: 61, 82: 42, 83: 58, 84: 31,
           85: 8, 86: 21, 87: 22, 88: 23, 89: 24, 90: 25, 97: 26, 98: 2,
           99: 28, 100: 12, 101: 30, 102: 19, 103: 10, 104: 33, 105: 49,
           106: 39, 107: 48, 108: 51, 109: 38, 110: 35, 111: 0, 112: 41,
           113: 17, 114: 43, 115: 3, 116: 45, 117: 46, 118: 7, 119: 36,
           120: 34, 121: 11, 122: 37}
HX_ALPHA = ["?"] * 64
for _ch, _v in _HX_DEC.items():
    HX_ALPHA[_v] = chr(_ch)
HX_ALPHA = "".join(HX_ALPHA)


def hx_b64e(data):
    out = []
    main = len(data) - len(data) % 3
    for i in range(0, main, 3):
        b0, b1, b2 = data[i], data[i + 1], data[i + 2]
        out += [HX_ALPHA[b0 >> 2], HX_ALPHA[((b0 & 3) << 4) + (b1 >> 4)],
                HX_ALPHA[((b1 & 0xF) << 2) + (b2 >> 6)], HX_ALPHA[b2 & 0x3F]]
    rem = len(data) % 3
    if rem == 1:
        b0 = data[-1]
        out += [HX_ALPHA[b0 >> 2], HX_ALPHA[(b0 & 3) << 4], "=", "="]
    elif rem == 2:
        b0, b1 = data[-2], data[-1]
        out += [HX_ALPHA[b0 >> 2], HX_ALPHA[((b0 & 3) << 4) + (b1 >> 4)],
                HX_ALPHA[(b1 & 0xF) << 2], "="]
    return "".join(out)


def hx_b64d(text):
    vals = []
    for ch in text:
        o = ord(ch)
        if ch == "=":
            vals.append(0)
        elif o in _HX_DEC:
            vals.append(_HX_DEC[o])
    out = bytearray()
    for i in range(0, len(vals) - 3, 4):
        v0, v1, v2, v3 = vals[i:i + 4]
        out += bytes([(v0 << 2) | (v1 >> 4), ((v1 & 15) << 4) | (v2 >> 2),
                      ((v2 & 3) << 6) | v3])
    return bytes(out)


def _save_secret(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.chmod(path, 0o600)


def _load_json_file(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _qs_sorted(d):
    return "&".join(f"{k}={d[k]}" for k in sorted(d))


def _qs_ordered(pairs):
    return "&".join(f"{k}={v}" for k, v in pairs)


def _fmt_num(x):
    s = ("%f" % float(x)).rstrip("0").rstrip(".")
    return s if s else "0"


def _mask_phone(phone):
    return phone[:3] + "****" + phone[-4:] if len(phone) == 11 else phone


def fetch_pubkey():
    text = http_raw(f"{AUTH_HOST}/verify2?reqtype=do_rsa&type=get_pubkey").decode(
        "gbk", errors="ignore")
    mod = re.search(r'modulus="([0-9A-Fa-f]+)"', text)
    exp = re.search(r'publicExponent="([0-9A-Fa-f]+)"', text)
    ver = re.search(r'rsa_version="([^"]+)"', text)
    if not (mod and exp and ver):
        fail(f"获取 RSA 公钥失败: {text[:160]}")
    return {"version": ver.group(1), "modulus": mod.group(1),
            "exponent": exp.group(1)}


def _rsa_pkcs1_encrypt(plain, n, e):
    k = (n.bit_length() + 7) // 8
    if len(plain) > k - 11:
        fail("待加密内容过长")
    ps = bytes(random.choice([x for x in range(1, 256)])
               for _ in range(k - len(plain) - 3))
    em = b"\x00\x02" + ps + b"\x00" + plain
    return pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")


def enc_field(value, pub):
    """客户端 EncryptString：GBK -> RSA PKCS#1 v1.5 -> 标准 Base64 -> URL 编码。
    注意：登录字段用的是标准 Base64（HevoHelper.HexinBase64Encode，经实机
    调用客户端 DLL 验证）；同花顺自定义字母表只用于自选接口（selfstock）。"""
    n = int(pub["modulus"], 16)
    e = int(pub["exponent"], 16)
    enc = _rsa_pkcs1_encrypt(value.encode("gbk"), n, e)
    return urllib.parse.quote(base64.b64encode(enc).decode("ascii"), safe="")


def img_size(data):
    """PNG/JPEG 宽高（纯标准库解析文件头）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8":  # JPEG: 扫描 SOF0/SOF2
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seglen
    fail("无法识别验证码图片格式")


def _json_loose(raw):
    text = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        fail(f"接口返回非 JSON: {text[:200]}")


def login_send_code(args):
    phone = (args.phone or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        fail("手机号格式不对（应为 11 位）")
    if args.captcha_x is None:
        # 第一步：取滑块验证码图片，交给调用方（agent）看图找缺口
        pre = _json_loose(http_raw(
            f"{CAPTCHA_BASE}/captcha/v2/getprehandle?appid=pc_yhb&captcha_type=4"))
        if str(pre.get("status_code")) != "0":
            fail(f"验证码预处理失败: {pre.get('status_msg') or pre}")
        d = pre["data"]
        os.makedirs(CAPTCHA_DIR, exist_ok=True)
        paths = {}
        sizes = {}
        for name, uid in (("bg", d["imgs"][0]), ("piece", d["imgs"][1])):
            blob = http_raw(f"{CAPTCHA_BASE}/getImg?{d['urlParams']}&iuk={uid}")
            ext = "png" if blob[:4] == b"\x89PNG" else "jpg"
            path = os.path.join(CAPTCHA_DIR, f"{name}.{ext}")
            with open(path, "wb") as f:
                f.write(blob)
            paths[name] = path
            sizes[name] = img_size(blob)
        flow = {"phone": phone, "urlParams": d["urlParams"], "sign": d["sign"],
                "bg_size": list(sizes["bg"]), "piece_size": list(sizes["piece"]),
                "initx": d.get("initx"), "inity": d.get("inity"),
                "stage": "captcha", "ts": time.time()}
        _save_secret(FLOW_PATH, flow)
        emit({"status": "captcha_required", "phone": _mask_phone(phone),
              "bg_image": paths["bg"], "piece_image": paths["piece"],
              "bg_size": flow["bg_size"], "piece_size": flow["piece_size"],
              "init": {"x": d.get("initx"), "y": d.get("inity")},
              "hint": "打开 bg_image 找到滑块缺口的左上角像素坐标 (x, y)，再执行 "
                      "ths login send-code --phone <手机号> --captcha-x <x> --captcha-y <y>；"
                      "拿不准时把 bg_image 发给用户确认"}, args)
        return
    # 第二步：带缺口坐标换 ticket 并发送短信
    flow = _load_json_file(FLOW_PATH)
    if not flow or flow.get("stage") != "captcha" or flow.get("phone") != phone:
        fail("没有待完成的滑块验证流程：先不带 --captcha-x 执行一次 send-code")
    if time.time() - flow.get("ts", 0) > 600:
        fail("滑块验证已过期（10 分钟），请重新执行 send-code")
    w, h = flow["bg_size"]
    y = args.captcha_y if args.captcha_y is not None else flow.get("inity", 0)
    phrase = f"{_fmt_num(args.captcha_x)};{_fmt_num(y)};{w};{h}"
    tick = _json_loose(http_raw(
        f"{CAPTCHA_BASE}/getTicket?{flow['urlParams']}&phrase={phrase}"))
    if str(tick.get("code")) != "0" or not tick.get("ticket"):
        os.path.exists(FLOW_PATH) and os.remove(FLOW_PATH)
        fail(f"滑块验证未通过: {tick.get('msg') or tick}（流程已重置，重新执行 send-code 取新图）")
    pub = fetch_pubkey()
    kvs = {"captcha_phrase": phrase, "captcha_signature": flow["sign"],
           "captcha_ticket": tick["ticket"], "captcha_type": "4",
           "country_code": "86", "mobile": enc_field(phone, pub),
           "request_type": "login", "rsa_version": pub["version"],
           "source": "pc_yhb"}
    resp = _json_loose(http_raw(f"{UPASS}/smslogin/sendSms?{_qs_sorted(kvs)}"))
    if str(resp.get("errorcode")) != "0":
        fail(f"发送短信失败: {resp.get('errormsg') or resp}")
    flow.update({"stage": "code_sent", "phrase": phrase,
                 "ticket": tick["ticket"], "pubkey": pub, "ts": time.time()})
    _save_secret(FLOW_PATH, flow)
    emit({"status": "code_sent", "phone": _mask_phone(phone),
          "hint": "向用户索取短信验证码，然后执行 ths login verify --code <验证码>"}, args)


def login_verify(args):
    flow = _load_json_file(FLOW_PATH)
    if not flow or flow.get("stage") != "code_sent":
        fail("没有待验证的短信流程：先执行 ths login send-code")
    if time.time() - flow.get("ts", 0) > 900:
        fail("短信验证码流程已过期（15 分钟），请重新 send-code")
    pub = flow["pubkey"]
    kvs = {"captcha_phrase": flow["phrase"], "captcha_signature": flow["sign"],
           "captcha_ticket": flow["ticket"], "captcha_type": "4",
           "country_code": "86", "longLogin": "1",
           "mobile": enc_field(flow["phone"], pub), "request_type": "login",
           "rsa_version": pub["version"], "signcode": enc_field(args.code.strip(), pub),
           "source": "pc_yhb"}
    raw = http_raw(f"{UPASS}/smslogin/checkSms",
                   data=_qs_sorted(kvs).encode("gbk"),
                   ctype="application/x-www-form-urlencoded")
    text = raw.decode("utf-8", errors="ignore")
    try:
        resp = json.loads(text)
    except ValueError:
        resp = _json_loose(raw)
    if str(resp.get("errorcode")) != "0":
        fail(f"验证码校验失败: {resp.get('errormsg') or resp}")
    account = resp.get("account")
    password = resp.get("passwd") or resp.get("password")
    if not account or not password:
        fail(f"checkSms 未返回账号凭据: {text[:200]}")
    _save_secret(AUTH_PATH, {"account": account, "password": password,
                              "expireTime": resp.get("expireTime"),
                              "phone": flow["phone"],
                              "saved_at": datetime.now().isoformat(timespec="seconds")})
    os.path.exists(FLOW_PATH) and os.remove(FLOW_PATH)
    emit({"status": "logged_in", "account": account,
          "expireTime": resp.get("expireTime"),
          "note": "凭据已存 ~/.ths_auth.json（0600，不外传）；云自选用 ths watchlist sync / push"}, args)


def cmd_login(args):
    if args.login_action == "send-code":
        login_send_code(args)
    elif args.login_action == "verify":
        login_verify(args)
    elif args.login_action == "status":
        auth = _load_json_file(AUTH_PATH)
        if auth:
            exp = auth.get("expireTime")
            exp_iso = None
            if isinstance(exp, (int, float)) and exp > 0:
                exp_iso = datetime.fromtimestamp(exp).isoformat(timespec="seconds")
            emit({"logged_in": True, "account": auth.get("account"),
                  "expireTime": exp, "expireTime_iso": exp_iso,
                  "phone": _mask_phone(auth.get("phone", "")),
                  "saved_at": auth.get("saved_at")}, args)
        else:
            emit({"logged_in": False}, args)
    elif args.login_action == "logout":
        for p in (AUTH_PATH, FLOW_PATH):
            if os.path.exists(p):
                os.remove(p)
        emit({"logged_in": False, "note": "已删除本机会话凭据"}, args)


def require_auth():
    auth = _load_json_file(AUTH_PATH)
    if not auth or not auth.get("account") or not auth.get("password"):
        fail("未登录：先执行 ths login send-code / verify 完成短信登录")
    return auth


def selfstock_call(pairs):
    body = "param=" + hx_b64e(_qs_ordered(pairs).encode("gbk"))
    raw = http_raw(SELFSTOCK_URL, data=body.encode("gbk"),
                   ctype="application/x-www-form-urlencoded")
    text = raw.decode("ascii", errors="ignore")
    payload = "".join(ch for ch in text if ch == "=" or ord(ch) in _HX_DEC)
    decoded = hx_b64d(payload).decode("gbk", errors="ignore")
    out = {}
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)=([^&]*)", decoded):
        out[m.group(1)] = m.group(2)
    if not out:
        fail(f"自选接口返回无法解析: {decoded[:160] or text[:160]}")
    return out


def _selfstock_get(auth):
    resp = selfstock_call([("do", "get"), ("account", auth["account"]),
                           ("passwd", auth["password"]), ("selfcode_crc", " "),
                           ("marketcode", "1"), ("expand", "1")])
    if str(resp.get("retcode")) != "0":
        fail(f"云自选拉取失败: {resp.get('ReturnMsg') or resp}")
    return resp


def watchlist_sync(args):
    auth = require_auth()
    resp = _selfstock_get(auth)
    parts = [p for p in re.split(r"[,\s]+", (resp.get("SelfStock") or "").strip()) if p]
    codes = parts[0].split("|") if parts else []
    oldids = parts[1].split("|") if len(parts) > 1 else []
    items = [{"code": c, "old_id": oldids[i] if i < len(oldids) else None}
             for i, c in enumerate(codes) if c]
    _save_secret(CLOUD_PATH, {"version": resp.get("Version"),
                              "rtime": resp.get("Rtime"), "items": items,
                              "synced_at": datetime.now().isoformat(timespec="seconds")})
    local = wl_load()
    merged = list(local) + [it["code"] for it in items if it["code"] not in local]
    wl_save(merged)
    emit({"source": "cloud", "account": auth["account"],
          "version": resp.get("Version"), "count": len(items), "items": items,
          "note": "已合并进本地自选（只增不删）；ths watchlist list 可看行情"}, args)


def watchlist_push(args):
    auth = require_auth()
    local = wl_load()
    if not local:
        fail("本地自选为空，无可推送")
    cloud = _load_json_file(CLOUD_PATH) or {"items": []}
    known = {it["code"]: it.get("old_id") for it in cloud.get("items", [])}
    market_old = {}
    for code, oid in known.items():
        if oid:
            _c, mid = normalize_code(code)
            if mid and mid not in market_old:
                market_old[mid] = oid
    olds, missing = [], []
    for code in local:
        _c, mid = normalize_code(code)
        oid = known.get(code) or market_old.get(mid or "")
        if oid:
            olds.append(oid)
        else:
            missing.append(code)
    if missing:
        fail(f"以下股票缺市场 old_id 映射，先跑一次 ths watchlist sync 让映射自学习: {missing}")
    ss = "|".join(local) + "," + "|".join(olds)
    resp = selfstock_call([("do", "put"), ("account", auth["account"]),
                           ("passwd", auth["password"]), ("selfstock", ss),
                           ("selfcode_crc", " ")])
    if str(resp.get("retcode", "0")) != "0":
        fail(f"云自选推送失败: {resp.get('ReturnMsg') or resp}")
    emit({"pushed": len(local), "codes": local, "response": resp}, args)


# ---------------------------------------------------------------- watchlist (local)

WL_PATH = os.environ.get(
    "THS_WATCHLIST",
    os.path.join(os.path.expanduser("~"), ".ths_watchlist.json"))


def wl_load():
    if os.path.exists(WL_PATH):
        return json.load(open(WL_PATH, encoding="utf-8"))
    return []


def wl_save(lst):
    json.dump(lst, open(WL_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def cmd_watchlist(args):
    if args.action == "sync":
        watchlist_sync(args)
        return
    if args.action == "push":
        watchlist_push(args)
        return
    lst = wl_load()
    if args.action == "add":
        code, _ = normalize_code(args.code)
        if code not in lst:
            lst.append(code)
            wl_save(lst)
        emit({"watchlist": lst}, args)
    elif args.action == "remove":
        code, _ = normalize_code(args.code)
        lst = [c for c in lst if c != code]
        wl_save(lst)
        emit({"watchlist": lst}, args)
    else:  # list（带实时行情）
        results = []
        for code in lst:
            try:
                q, _ = fetch_quote(code)
                results.append(q)
            except Exception as e:
                results.append({"code": code, "error": str(e)})
        emit({"watchlist": results}, args,
             cols=["code", "name", "price", "change_pct", "amount"])


# ---------------------------------------------------------------- output

def emit(data, args, cols=None):
    if getattr(args, "table", False):
        rows = data
        if isinstance(data, dict):
            for k in ("results", "items", "watchlist"):
                if k in data:
                    meta = {kk: vv for kk, vv in data.items() if kk != k}
                    if meta:
                        print(f"# {meta}")
                    rows = data[k]
                    break
            else:
                rows = [data]
        if isinstance(rows, dict):
            rows = [rows]
        cols = cols or (list(rows[0].keys()) if rows else [])
        print("\t".join(str(c) for c in cols))
        for r in rows:
            print("\t".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="ths", description="同花顺 CLI（agent 友好，默认 JSON 输出）")
    p.add_argument("--table", action="store_true", help="表格输出代替 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("quote", help="实时行情快照（可多只）")
    sp.add_argument("codes", nargs="+")
    sp.add_argument("--raw", action="store_true", help="附带原始字段号字典")
    sp.set_defaults(func=cmd_quote)

    sp = sub.add_parser("kline", help="K 线历史")
    sp.add_argument("code")
    sp.add_argument("--period", default="day", choices=list(PERIODS))
    sp.add_argument("--count", type=int, default=30)
    sp.set_defaults(func=cmd_kline)

    sp = sub.add_parser("intraday", help="当日分时")
    sp.add_argument("code")
    sp.set_defaults(func=cmd_intraday)

    sp = sub.add_parser("search", help="按名称/关键词找股票（客户端 NLP 接口）")
    sp.add_argument("keyword")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("screen", help="自然语言选股，如 '今日涨幅最大的股票'")
    sp.add_argument("condition")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_screen)

    sp = sub.add_parser("news", help="资讯（客户端内置 RSS 源）")
    sp.add_argument("--source", default="cjzx", choices=list(NEWS_SOURCES))
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_news)

    sp = sub.add_parser("f10", help="F10 公司资料 + 财务指标摘要（--deep 附财报三表核心指标 + 十大股东）")
    sp.add_argument("code")
    sp.add_argument("--deep", action="store_true")
    sp.set_defaults(func=cmd_f10)

    sp = sub.add_parser("rank", help="全市场排行（问财 NLP + 行情重排：涨幅/跌幅/成交额/换手/成交量/资金流净流入/净流出）")
    sp.add_argument("--by", default="change",
                    choices=["change", "fall", "amount", "turnover", "volume",
                             "fund", "fundout"])
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_rank)

    sp = sub.add_parser("lhb", help="龙虎榜名单（最近交易日，问财；无营业部明细）")
    sp.add_argument("--limit", type=int, default=100)
    sp.set_defaults(func=cmd_lhb)

    sp = sub.add_parser("sector", help="板块：list 列表 / quote 板块指数行情 / stocks 成分股")
    sp.add_argument("action", choices=["list", "quote", "stocks"])
    sp.add_argument("target", nargs="?", help="板块名或 6 位板块代码（quote/stocks 用）")
    sp.add_argument("--type", default="all", choices=["all", "industry", "concept"])
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--refresh", action="store_true", help="重扫板块注册表")
    sp.add_argument("--raw", action="store_true")
    sp.set_defaults(func=cmd_sector)

    sp = sub.add_parser("watchlist", help="自选股（list/add/remove 本地；sync/push 云端，需登录）")
    sp.add_argument("action", choices=["list", "add", "remove", "sync", "push"])
    sp.add_argument("code", nargs="?")
    sp.set_defaults(func=cmd_watchlist)

    sp = sub.add_parser("login", help="同花顺账号登录（短信验证码，凭据只存本机）")
    lsub = sp.add_subparsers(dest="login_action", required=True)
    l1 = lsub.add_parser("send-code", help="发短信验证码（先不带 --captcha-x 取滑块图，看图找缺口后再带坐标重跑）")
    l1.add_argument("--phone", required=True)
    l1.add_argument("--captcha-x", type=float, default=None)
    l1.add_argument("--captcha-y", type=float, default=None)
    l2 = lsub.add_parser("verify", help="用短信验证码完成登录")
    l2.add_argument("--code", required=True)
    lsub.add_parser("status", help="查看登录状态")
    lsub.add_parser("logout", help="删除本机凭据")
    sp.set_defaults(func=cmd_login)

    argv = sys.argv[1:]
    table_flag = "--table" in argv  # --table 允许出现在任意位置
    argv = [a for a in argv if a != "--table"]
    args = p.parse_args(argv)
    args.table = table_flag
    if args.cmd == "watchlist" and args.action in ("add", "remove") and not args.code:
        fail("watchlist add/remove 需要股票代码")
    try:
        args.func(args)
    except BrokenPipeError:
        pass
    except Exception as e:
        fail(str(e))


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
