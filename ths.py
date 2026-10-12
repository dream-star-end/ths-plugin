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

def _urlopen_retry(req):
    """网关偶发 502/503 与瞬时网络错误时重试（3 次，退避 0.7s/1.4s）；
    agent 调用不应被单次瞬时故障打断。"""
    import time as _time
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (502, 503, 504):
                raise
        except urllib.error.URLError as e:
            last = e
        if attempt < 2:
            _time.sleep(0.7 * (attempt + 1))
    raise last


def http_get(url, referer=None, encoding=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    raw = _urlopen_retry(req)
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
    if data is not None:
        # POST 不自动重试（避免重复提交，如发短信/推送自选）
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    return _urlopen_retry(req)


def fail(msg, code="ERROR"):
    """统一错误出口：stderr 输出 {"error": msg, "code": code} 并以退出码 1 结束，
    code 供 agent 分支处理（INVALID_CODE/NOT_FOUND/UPSTREAM/NETWORK/AUTH 等）。"""
    print(json.dumps({"error": msg, "code": code}, ensure_ascii=False),
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- tiny cache

CACHE_PATH = os.path.join(os.path.expanduser("~"), ".ths_cache.json")


def _cache_get(key, ttl):
    """短期文件缓存：降低 agent 一轮分析内的重复请求（行情 20s、K 线 30min、
    财报 6h）。THS_NO_CACHE=1 可禁用。缓存损坏时静默回退为未命中。"""
    if os.environ.get("THS_NO_CACHE"):
        return None
    try:
        import time
        with open(CACHE_PATH, encoding="utf-8") as f:
            ent = json.load(f).get(key)
        if ent and time.time() - ent["ts"] < ttl:
            return ent["data"]
    except Exception:
        pass
    return None


def _cache_set(key, data):
    if os.environ.get("THS_NO_CACHE"):
        return
    try:
        import time
        store = {}
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, encoding="utf-8") as f:
                store = json.load(f)
        now = time.time()
        store = {k: v for k, v in store.items() if now - v["ts"] < 86400}
        store[key] = {"ts": now, "data": data}
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------- code utils

MARKET_ID = {"17": "SH", "33": "SZ", "151": "BJ"}


def _normalize_code(raw):
    """normalize_code 的内部实现：无法识别时抛 ValueError（不打印、不退出），
    供需要静默试探的调用方使用（如指数代码 1A0001 的 old_id 回退）。"""
    s = str(raw).strip().upper()
    market = None
    m = re.match(r"^(SH|SZ|BJ)?(\d{6})(?:\.(SH|SZ|BJ|17|33))?$", s)
    if not m:
        raise ValueError(f"无法识别的股票代码: {raw}")
    pre, code, suf = m.group(1), m.group(2), m.group(3)
    tag = suf or pre
    if tag in ("SH", "17"):
        market = "17"
    elif tag in ("SZ", "33"):
        market = "33"
    elif tag == "BJ":
        market = "151"  # 北交所，v6 网关同样走 hs_ 前缀时可回退
    if market is None:  # 按代码段推断
        if code.startswith("920"):
            market = "151"  # 北交所（920xxx；网关同样走 hs_ 前缀，实测 920185 贝特瑞可查）
        elif code.startswith(("6", "9")):
            market = "17"
        elif code.startswith(("0", "2", "3")):
            market = "33"
    return code, market


def normalize_code(raw):
    """600519 / sh600519 / 600519.SH / 600519.17 -> (code, market_id|None)"""
    try:
        return _normalize_code(raw)
    except ValueError as e:
        fail(str(e), code="INVALID_CODE")


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
    hit = _cache_get(f"quote:{code}", ttl=20)
    if hit is not None:
        return hit["out"], hit["items"]
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
    _cache_set(f"quote:{code}", {"out": out, "items": items})
    return out, items


def cmd_quote(args):
    results = []
    for raw in args.codes:
        try:
            code, _ = _normalize_code(raw)
        except ValueError:
            # 指数代码（如上证指数 1A0001）不是 6 位数字股票代码，
            # 网关 hs_ 符号可直接查，绕过股票代码规范化
            if re.fullmatch(r"[0-9A-Z]{6}", str(raw).strip().upper()):
                code = str(raw).strip().upper()
            else:
                fail(f"无法识别的股票代码: {raw}")
        if code.startswith(("881", "885")) and \
                code in load_sectors()["sectors"]:
            # 板块代码直查：走板块指数行情（bk_ 网关）
            info = load_sectors()["sectors"][code]
            q = fetch_block_quote(code)
            q.pop("raw_items", None)
            q.update({"name": info["name"], "type": info["type"],
                      "target_type": "sector"})
            results.append(q)
            continue
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


def fetch_kline(code, period, count, symbol=None):
    symbol = symbol or v6_symbol(code)
    ttl = 1800 if period in ("day", "week", "month") else 120
    ckey = f"kline:{symbol}:{period}:{count}"
    hit = _cache_get(ckey, ttl=ttl)
    if hit is not None:
        return hit
    p = PERIODS[period]
    data = http_jsonp(
        f"https://d.10jqka.com.cn/v6/line/{symbol}/{p}/last{count}.js",
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
    # 复权口径：已用比亚迪(002594)验证——2025-07-29 送转除权日(10送8转12)
    # 前后收盘价连续无 1/3 跳空，网关个股日线为前复权（板块指数无复权概念）。
    out = {"code": code, "name": data.get("name", ""), "symbol": symbol,
           "period": period,
           "adjust": "forward" if symbol.startswith("hs_") else None,
           "rows": rows[-count:]}
    _cache_set(ckey, out)
    return out


def _resolve_kline_target(raw):
    """K线/指标的目标解析：股票代码、指数代码、板块代码（881xxx/885xxx）、
    板块名、bk_881101 写法 → (展示代码, v6 符号, 名称提示, 是否板块)。"""
    s = str(raw).strip()
    if s.lower().startswith("bk_"):
        c = s[3:]
        info = load_sectors()["sectors"].get(c, {})
        return c, f"bk_{c}", info.get("name", ""), True
    try:
        code, _ = _normalize_code(s)
    except ValueError:
        code = None
    if code:
        if code.startswith(("881", "885")):
            info = load_sectors()["sectors"].get(code)
            if info:
                return code, f"bk_{code}", info["name"], True
        return code, v6_symbol(code), "", False
    if re.fullmatch(r"[0-9A-Z]{6}", s.upper()):
        return s.upper(), v6_symbol(s.upper()), "", False
    code, name, _t = resolve_sector(s, load_sectors())
    return code, f"bk_{code}", name, True


def cmd_kline(args):
    code, symbol, hint, _is_sector = _resolve_kline_target(args.code)
    k = fetch_kline(code, args.period, args.count, symbol=symbol)
    if not k["name"] and hint:
        k["name"] = hint
    emit(k, args)


def cmd_intraday(args):
    code, symbol, hint, is_sector = _resolve_kline_target(args.code)
    data = http_jsonp(
        f"https://d.10jqka.com.cn/v6/time/{symbol}/last.js",
        referer=f"https://stockpage.10jqka.com.cn/{code}/",
    )
    node = data.get(symbol, {})

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
    emit({"code": code, "name": node.get("name", "") or hint,
          "target_type": "sector" if is_sector else "stock",
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
    from concurrent.futures import ThreadPoolExecutor

    def one(cm):
        code, _mid = cm
        try:
            q, _ = fetch_quote(code)
            return q
        except Exception as e:  # 单只失败不拖垮整批
            return {"code": code, "error": str(e)}
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, codes[:args.limit]))
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
        payload["deep"] = cmd_f10_deep(code, all_rows=getattr(args, "all", False))
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
    hit = _cache_get(f"stmt:{code}:{kind}:{periods_n}", ttl=21600)
    if hit is not None:
        return hit
    text = http_get(f"https://basic.10jqka.com.cn/api/stock/finance/{code}_{kind}.json")
    inner = json.loads(json.loads(text)["flashData"])
    titles, report = inner["title"], inner["report"]
    yoy_mat = inner.get("report_yoy") or []
    periods = [p for p in report[0] if p][:periods_n]
    rows = []
    for i in range(1, len(titles)):
        t = titles[i]
        if not isinstance(t, list) or i >= len(report):
            continue
        name = str(t[0])
        vals = report[i]
        yoy = yoy_mat[i] if i < len(yoy_mat) else []
        rows.append({"item": name.lstrip("*"), "core": name.startswith("*"),
                     "unit": t[1] if len(t) > 1 else "",
                     "values": {periods[j]: _num(vals[j])
                                for j in range(len(periods)) if j < len(vals)},
                     "yoy_pct": {periods[j]: _num(yoy[j])
                                 for j in range(len(periods)) if j < len(yoy)}})
    out = {"periods": periods, "rows": rows}
    _cache_set(f"stmt:{code}:{kind}:{periods_n}", out)
    return out


def _td_texts(tr_html):
    """一行内的单元格文本（股东名在 <th> 里，其余在 <td>，需按序全取）。"""
    cells = []
    for td in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr_html, re.S):
        txt = re.sub(r"<[^>]+>", " ", td)
        cells.append(re.sub(r"\s+", " ", txt).strip())
    return cells


def fetch_holders(code):
    """十大股东：basic F10 holder.html 服务端渲染表格（取最新一期）。"""
    hit = _cache_get(f"holders:{code}", ttl=21600)
    if hit is not None:
        return hit
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
    _cache_set(f"holders:{code}", out)
    return out


def cmd_f10_deep(code, all_rows=False):
    fin = {}
    for kind in ("benefit", "debt", "cash"):
        try:
            st = fetch_statement(code, kind)
            entry = {"periods": st["periods"],
                     "core_rows": [r for r in st["rows"] if r["core"]],
                     "total_rows": len(st["rows"])}
            if all_rows:
                entry["rows"] = st["rows"]
            fin[kind] = entry
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
        fail(f"板块名不唯一，请用代码: {[(c, v['name']) for c, v in fuzzy[:10]]}",
             code="AMBIGUOUS")
    fail(f"板块注册表里找不到: {q}（可先 ths sector list --refresh 刷新）",
         code="NOT_FOUND")


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


# ---------------------------------------------------------------- events

def cmd_events(args):
    """公司大事·近期重要事件：basic F10 event.html 的「近期重要事件」区
    （股东大会/业绩披露/股票回购/分红方案/限售解禁等，页面服务端渲染）。"""
    code, _ = normalize_code(args.code)
    text = _page_text(f"https://basic.10jqka.com.cn/{code}/event.html")
    # 页面导航里也有「近期重要事件」字样，真正的区块标题后紧跟日期
    m_sec = re.search(r"近期重要事件\s*(?=\d{4}-\d{2}-\d{2})", text)
    idx = m_sec.start() if m_sec else text.find("近期重要事件")
    seg = text[idx:idx + 9000] if idx >= 0 else text[:9000]
    pat = re.compile(
        r"(\d{4}-\d{2}-\d{2})\s*([^：:\s]{2,10})：\s*"
        r"(.+?)(?=\s*\d{4}-\d{2}-\d{2}\s*[^：:]{2,10}：|详细内容|$)")
    import html as _html
    events = []
    for m in pat.finditer(seg):
        body = _html.unescape(re.sub(r"\s+", " ", m.group(3))).strip()
        body = re.split(r"(详细内容|展开|收起)", body)[0].strip(" ▼▲·")
        body = re.sub(r"^(更多>>|详情>>|更多|详情)\s*", "", body)
        if body:
            events.append({"date": m.group(1), "category": m.group(2),
                           "text": body[:220]})
        if len(events) >= args.limit:
            break
    emit({"code": code, "events": events,
          "source": "basic.10jqka.com.cn F10 公司大事·近期重要事件"}, args,
         cols=["date", "category", "text"])


# ---------------------------------------------------------------- indicators / doctor / manifest

def _r3(x):
    return round(x, 3) if isinstance(x, float) else x


def _ema_series(vals, n):
    k = 2 / (n + 1)
    out, cur = [], None
    for v in vals:
        cur = v if cur is None else v * k + cur * (1 - k)
        out.append(cur)
    return out


def compute_indicators(rows):
    """在 K 线 rows 上计算 MA/MACD/RSI/KDJ/BOLL，返回与 rows 等长的列表。"""
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    ema12, ema26 = _ema_series(closes, 12), _ema_series(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema_series(dif, 9)
    # RSI(14) Wilder 平滑
    rsi = [None] * len(closes)
    if len(closes) > 14:
        gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
        ag = sum(gains[:14]) / 14
        al = sum(losses[:14]) / 14
        rsi[14] = 100 - 100 / (1 + (ag / al if al else float("inf")))
        for i in range(15, len(closes)):
            ag = (ag * 13 + gains[i - 1]) / 14
            al = (al * 13 + losses[i - 1]) / 14
            rsi[i] = 100 - 100 / (1 + (ag / al if al else float("inf")))
    out = []
    kk = dd = 50.0
    for i, r in enumerate(rows):
        e = {"date": r["date"], "close": r["close"]}
        for n in (5, 10, 20, 60):
            e[f"ma{n}"] = _r3(sum(closes[i - n + 1:i + 1]) / n) if i >= n - 1 else None
        e["dif"] = _r3(dif[i])
        e["dea"] = _r3(dea[i])
        e["macd_hist"] = _r3(2 * (dif[i] - dea[i]))
        e["rsi14"] = _r3(rsi[i]) if rsi[i] is not None else None
        if i >= 8:
            llv = min(lows[i - 8:i + 1])
            hhv = max(highs[i - 8:i + 1])
            rsv = (closes[i] - llv) / (hhv - llv) * 100 if hhv > llv else 50.0
            kk = 2 / 3 * kk + 1 / 3 * rsv
            dd = 2 / 3 * dd + 1 / 3 * kk
            e["kdj_k"], e["kdj_d"], e["kdj_j"] = _r3(kk), _r3(dd), _r3(3 * kk - 2 * dd)
        else:
            e["kdj_k"] = e["kdj_d"] = e["kdj_j"] = None
        if i >= 19:
            import statistics
            win = closes[i - 19:i + 1]
            mid = sum(win) / 20
            sd = statistics.pstdev(win)
            e["boll_mid"], e["boll_upper"], e["boll_lower"] = \
                _r3(mid), _r3(mid + 2 * sd), _r3(mid - 2 * sd)
        else:
            e["boll_mid"] = e["boll_upper"] = e["boll_lower"] = None
        out.append(e)
    return out


def cmd_indicators(args):
    code, symbol, hint, is_sector = _resolve_kline_target(args.code)
    k = fetch_kline(code, args.period, 300, symbol=symbol)
    rows = k["rows"]
    if len(rows) < 30:
        fail(f"K 线数据不足（{len(rows)} 根），无法计算指标")
    series = compute_indicators(rows)
    tail = series[-args.count:]
    emit({"code": code, "name": k["name"] or hint,
          "target_type": "sector" if is_sector else "stock",
          "period": args.period,
          "adjust": k.get("adjust"), "bars_used": len(rows),
          "as_of": rows[-1]["date"], "latest": tail[-1], "series": tail},
         args)


def cmd_doctor(args):
    """通道健康检查：供 agent 自检（接口均为非官方，失效要能第一时间发现）。"""
    import time as _time
    checks = []

    def run(name, fn, critical=True):
        t0 = _time.time()
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "critical": critical,
                           "ms": int((_time.time() - t0) * 1000),
                           "detail": detail})
        except BaseException as e:  # nlp 失败走 fail() 的 SystemExit
            checks.append({"name": name, "ok": False, "critical": critical,
                           "ms": int((_time.time() - t0) * 1000),
                           "detail": str(e)[:200]})

    def c_quote():
        q, _ = fetch_quote("600519")
        assert q.get("price"), "no price"
        return f"{q.get('name')} price={q['price']} update_time={q.get('update_time')}"

    def c_kline():
        k = fetch_kline("600519", "day", 5)
        assert len(k["rows"]) >= 5, "bars<5"
        return f"bars={len(k['rows'])} last={k['rows'][-1]['date']}"

    def c_consistency():
        q, _ = fetch_quote("600519")
        k = fetch_kline("600519", "day", 2)
        diff = abs(k["rows"][-1]["close"] - q["price"]) / q["price"] * 100
        assert diff < 2, f"quote/kline close diff {diff:.2f}%"
        return f"quote_vs_kline_close_diff={diff:.3f}%"

    def c_nlp():
        chunks, codes = nlp_query("贵州茅台")
        assert codes, "no hits"
        return f"hits={len(codes)} parsed={chunks[:1]}"

    def c_news():
        raw = http_get(NEWS_SOURCES["cjzx"][1], encoding="gbk")
        assert "<item>" in raw, "no rss items"
        return "rss ok"

    def c_f10():
        st = fetch_statement("600519", "benefit")
        assert st["periods"], "no periods"
        return f"latest_period={st['periods'][0]}"

    def c_sector():
        n = len(load_sectors()["sectors"])
        assert n > 400, f"sectors={n}"
        return f"sectors={n}"

    def c_login():
        auth = _load_json_file(AUTH_PATH)
        if not auth:
            return "not logged in (optional, login only needed for cloud watchlist)"
        exp = auth.get("expireTime")
        left = ""
        if isinstance(exp, (int, float)) and exp > 0:
            days = (exp - _time.time()) / 86400
            left = f", {days:.1f} days left"
        return f"account={auth.get('account')}{left}"

    run("gateway_quote", c_quote)
    run("gateway_kline", c_kline)
    run("quote_kline_consistency", c_consistency)
    run("wencai_nlp", c_nlp)
    run("news_rss", c_news)
    run("f10_finance", c_f10)
    run("sector_registry", c_sector)
    run("login", c_login, critical=False)
    ok = all(c["ok"] for c in checks if c["critical"])
    emit({"ok": ok, "checks": checks}, args)
    if not ok:
        sys.exit(1)


COMMANDS_MANIFEST = [
    {"name": "quote", "usage": "ths quote <code...> [--raw]",
     "desc": "实时行情快照：现价/涨跌幅/量额/换手/涨跌停/市值 + order_book 买一卖一与委比（仅一档）"},
    {"name": "kline", "usage": "ths kline <code> [--period day|week|month|60m|30m|15m|5m] [--count N]",
     "desc": "K线（前复权，输出含 adjust=forward）"},
    {"name": "intraday", "usage": "ths intraday <code>", "desc": "当日 1 分钟分时"},
    {"name": "indicators", "usage": "ths indicators <code> [--period day] [--count N]",
     "desc": "技术指标：MA5/10/20/60、MACD、RSI14、KDJ、BOLL（本地计算，基于前复权K线）"},
    {"name": "search", "usage": "ths search <关键词>", "desc": "按名称/代码找股"},
    {"name": "screen", "usage": "ths screen \"<自然语言条件>\" [--limit N]",
     "desc": "问财 NLP 自然语言选股"},
    {"name": "news", "usage": "ths news [--source cjzx|hk|hgt|whfx|global] [--limit N]",
     "desc": "财经资讯 RSS"},
    {"name": "f10", "usage": "ths f10 <code> [--deep] [--all]",
     "desc": "F10 摘要；--deep 附财报三表核心指标（近4期，含同比 yoy_pct）+十大股东；--all 附全量科目"},
    {"name": "rank", "usage": "ths rank [--by change|fall|amount|turnover|volume|fund|fundout] [--limit N]",
     "desc": "全市场排行（近似榜单）；fund/fundout 为主力资金名单版，无金额数值"},
    {"name": "lhb", "usage": "ths lhb [--limit N]",
     "desc": "龙虎榜名单（最近交易日；无上榜原因/营业部明细）"},
    {"name": "sector", "usage": "ths sector list|quote|stocks [板块名|代码] [--type industry|concept] [--limit N] [--refresh]",
     "desc": "板块列表（95行业+431概念）/板块指数行情/成分股"},
    {"name": "watchlist", "usage": "ths watchlist list|add|remove|sync|push [code]",
     "desc": "本地自选（list 带行情）；sync 云→本地合并、push 本地→云（需登录）"},
    {"name": "login", "usage": "ths login send-code --phone N [--captcha-x X] | verify --code C | status | logout",
     "desc": "同花顺短信登录（滑块验证码需看图定位；凭据存 ~/.ths_auth.json，约30天）"},
    {"name": "events", "usage": "ths events <code> [--limit N]",
     "desc": "公司大事·近期重要事件（股东大会/业绩披露/回购/分红/解禁等，来自 F10 event 页）"},
    {"name": "doctor", "usage": "ths doctor",
     "desc": "通道健康检查（行情/K线/问财/资讯/F10/板块/登录），有关键项失败时退出码 1"},
    {"name": "commands", "usage": "ths commands", "desc": "本能力清单（机器可读）"},
]


def cmd_commands(args):
    emit({"commands": COMMANDS_MANIFEST,
          "conventions": "默认 JSON 输出；--table 切表格；错误为 stderr 的 "
                         "{\"error\": ...} 且退出码 1；非交易时段返回最近收盘，"
                         "先看 status/update_time"}, args)


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

    def _mid(code):
        # 指数代码（如上证指数 1A0001）不是标准股票代码，normalize 会失败；
        # 这类代码以云端快照里的 old_id 为准，不走市场回退
        try:
            return _normalize_code(code)[1]
        except ValueError:
            return None
    market_old = {}
    for code, oid in known.items():
        if oid:
            mid = _mid(code)
            if mid and mid not in market_old:
                market_old[mid] = oid
    olds, missing = [], []
    for code in local:
        oid = known.get(code) or market_old.get(_mid(code) or "")
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
    else:  # list（带实时行情，并发拉取、保持原顺序，单只失败不拖垮整批）
        from concurrent.futures import ThreadPoolExecutor

        def one(code):
            try:
                q, _ = fetch_quote(code)
                return q
            except Exception as e:
                return {"code": code, "error": str(e)}
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(one, lst))
        emit({"watchlist": results}, args,
             cols=["code", "name", "price", "change_pct", "amount"])


# ---------------------------------------------------------------- output

_LIST_KEYS = ("results", "watchlist", "items", "series", "sectors", "checks",
              "events")


def _pick_fields(obj, fields):
    """--fields 字段裁剪：顶层 dict 只保留所选键；结果列表容器
    （results/watchlist/...）保留并逐行裁剪，元信息键随之丢弃。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _LIST_KEYS and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in row.items() if kk in fields}
                          if isinstance(row, dict) else row for row in v]
            elif k in fields:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [{k: v for k, v in row.items() if k in fields}
                if isinstance(row, dict) else row for row in obj]
    return obj


def emit(data, args, cols=None):
    if getattr(args, "table", False):
        rows = data
        if isinstance(data, dict):
            for k in ("results", "items", "watchlist", "events"):
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
        fields = getattr(args, "fields", None)
        if fields:
            data = _pick_fields(data, set(fields))
        if getattr(args, "compact", False):
            print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="ths", description="同花顺 CLI（agent 友好，默认 JSON 输出）")
    p.add_argument("--version", action="version", version="ths 1.3.0")
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
    sp.add_argument("--all", action="store_true", help="配合 --deep 输出三表全量科目")
    sp.set_defaults(func=cmd_f10)

    sp = sub.add_parser("indicators", help="技术指标（MA/MACD/RSI/KDJ/BOLL，基于前复权 K 线本地计算）")
    sp.add_argument("code")
    sp.add_argument("--period", default="day",
                    choices=["day", "week", "month", "60m", "30m", "15m", "5m"])
    sp.add_argument("--count", type=int, default=5, help="输出最近 N 根的指标序列")
    sp.set_defaults(func=cmd_indicators)

    sp = sub.add_parser("doctor", help="通道健康检查（agent 自检用）")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("commands", help="能力清单（机器可读 JSON）")
    sp.set_defaults(func=cmd_commands)

    sp = sub.add_parser("rank", help="全市场排行（问财 NLP + 行情重排：涨幅/跌幅/成交额/换手/成交量/资金流净流入/净流出）")
    sp.add_argument("--by", default="change",
                    choices=["change", "fall", "amount", "turnover", "volume",
                             "fund", "fundout"])
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_rank)

    sp = sub.add_parser("lhb", help="龙虎榜名单（最近交易日，问财；无营业部明细）")
    sp.add_argument("--limit", type=int, default=100)
    sp.set_defaults(func=cmd_lhb)

    sp = sub.add_parser("events", help="公司大事·近期重要事件（股东大会/业绩披露/回购/分红/解禁）")
    sp.add_argument("code")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_events)

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
    # --table / --compact / --fields 允许出现在任意位置，先行提取
    table_flag = "--table" in argv
    compact_flag = "--compact" in argv
    fields = None
    cleaned = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--table", "--compact"):
            i += 1
        elif a == "--fields" and i + 1 < len(argv):
            fields = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        elif a.startswith("--fields="):
            fields = [x.strip() for x in a[9:].split(",") if x.strip()]
            i += 1
        else:
            cleaned.append(a)
            i += 1
    args = p.parse_args(cleaned)
    args.table = table_flag
    args.compact = compact_flag
    args.fields = fields
    if args.cmd == "watchlist" and args.action in ("add", "remove") and not args.code:
        fail("watchlist add/remove 需要股票代码")
    try:
        args.func(args)
    except BrokenPipeError:
        pass
    except Exception as e:
        code = "INTERNAL"
        if isinstance(e, urllib.error.HTTPError):
            code = {404: "NOT_FOUND", 429: "RATE_LIMIT"}.get(
                e.code, "UPSTREAM" if e.code >= 500 else "HTTP")
        elif isinstance(e, urllib.error.URLError):
            code = "NETWORK"
        elif isinstance(e, RuntimeError):
            code = "UPSTREAM"
        fail(str(e), code=code)


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
