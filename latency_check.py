#!/usr/bin/env python3
"""ths 实时行情延迟实测（纯标准库）。

用户的问题：同花顺 Web 网关的实时行情到底延迟多少？
测法（盘中跑才有意义）：
  1. 新鲜度滞后：每轮拉一次自选行情，把网关返回的 update_time
     和本机抓取时刻（Asia/Shanghai）做差，统计 min/中位/max。
  2. 跳动间隔：记录同一只股票 price/update_time 发生变化的观测时刻，
     给出相邻变化的间隔参考。
网关被挡（403 / Tunnel）时明确报 egress_blocked，不拿旧数据冒充实时。

用法：
  ./latency_check.py                 # 默认自选 18 只，5 轮，间隔 3 秒
  ./latency_check.py --codes 600519 000001 --rounds 10 --interval 2
输出 JSON 到 stdout，人读摘要到 stderr。
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
THS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths")
WL_PATH = os.environ.get(
    "THS_WATCHLIST", os.path.expanduser("~/.ths_watchlist.json"))


def parse_update_time(raw, now):
    """update_time 格式不固定：逐个格式试，都不行返回 None（保留原文）。"""
    if raw in (None, ""):
        return None
    s = str(raw).strip()
    # epoch 秒/毫秒
    if s.isdigit() and len(s) >= 10:
        v = int(s)
        if v > 10**12:
            v //= 1000
        return datetime.fromtimestamp(v, tz=CST)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CST)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
            return now.replace(hour=t.hour, minute=t.minute,
                               second=t.second if fmt == "%H:%M:%S" else 0,
                               microsecond=0)
        except ValueError:
            pass
    return None


def fetch(codes):
    out = subprocess.run([THS, "quote", *codes], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    data = json.loads(out.stdout)
    return data if isinstance(data, list) else [data]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=None,
                    help="默认读本地自选 ~/.ths_watchlist.json")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    codes = args.codes
    if not codes:
        with open(WL_PATH, encoding="utf-8") as f:
            codes = json.load(f)

    lags = {}          # code -> [lag_seconds]
    raw_times = {}     # code -> last raw update_time (parse 失败时留证)
    last_seen = {}     # code -> (price, update_time)
    change_gaps = {}   # code -> [seconds between observed changes]
    last_change_at = {}
    errors = []

    for r in range(args.rounds):
        fetched_at = datetime.now(CST)
        try:
            quotes = fetch(codes)
        except Exception as e:  # 网关/网络层失败：整轮作废并记录
            errors.append({"round": r, "error": str(e)})
            quotes = []
        for q in quotes:
            code = q.get("code")
            if "error" in q:
                errors.append({"round": r, "code": code,
                               "error": str(q["error"])})
                continue
            ut = parse_update_time(q.get("update_time"), fetched_at)
            if ut is not None:
                lags.setdefault(code, []).append(
                    round((fetched_at - ut).total_seconds(), 2))
            else:
                raw_times[code] = q.get("update_time")
            key = (q.get("price"), q.get("update_time"))
            if code in last_seen and last_seen[code] != key:
                if code in last_change_at:
                    change_gaps.setdefault(code, []).append(
                        round((fetched_at - last_change_at[code])
                              .total_seconds(), 2))
                last_change_at[code] = fetched_at
            last_seen[code] = key
        if r < args.rounds - 1:
            time.sleep(args.interval)

    blocked = bool(errors) and not lags and not last_seen
    summary = {}
    for code, vals in sorted(lags.items()):
        summary[code] = {
            "samples": len(vals),
            "lag_min_s": min(vals),
            "lag_median_s": statistics.median(vals),
            "lag_max_s": max(vals),
        }
    all_lags = [v for vals in lags.values() for v in vals]
    result = {
        "checked_at": datetime.now(CST).isoformat(),
        "codes": codes,
        "rounds": args.rounds,
        "verdict": ("egress_blocked" if blocked else
                    "ok" if all_lags else "no_parseable_update_time"),
        "overall_lag_s": (
            {"min": min(all_lags), "median": statistics.median(all_lags),
             "max": max(all_lags)} if all_lags else None),
        "per_code": summary,
        "change_gaps_s": change_gaps,
        "unparsed_update_time": raw_times,
        "errors": errors[:20],
        "note": ("非交易时段 update_time 停在收盘时刻，滞后会虚高；"
                 "结论只认盘中（9:30-11:30 / 13:00-15:00）样本。"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    ov = result["overall_lag_s"]
    if result["verdict"] == "egress_blocked":
        print("结论：网关被挡（403），本轮测不到延迟，不是 CLI 坏了。",
              file=sys.stderr)
    elif ov:
        print(f"结论：update_time 滞后 min {ov['min']}s / 中位 "
              f"{ov['median']}s / max {ov['max']}s（{len(all_lags)} 个样本）",
              file=sys.stderr)
    else:
        print("结论：拉到了行情但 update_time 格式没解析出来，看 "
              "unparsed_update_time 原文再补格式。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
