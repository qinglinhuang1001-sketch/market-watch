#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
分开输出：
- 基金净值（场外）：reports/nav/<净值日期>.csv
- ETF 行情（场内）：reports/quotes/<交易日期>.csv

基金数据源：Eastmoney pingzhongdata
ETF 数据源：新浪行情（hq.sinajs.cn）
文件写入：幂等合并（同日重复运行只覆盖相同 code 的行）
日志：逐标的抓取日志 + 错误 traceback + CSV 预览
"""

import os
import re
import csv
import json
import sys
import traceback
import requests
from collections import OrderedDict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Tuple, List, Dict

# ===== 配置 =====
FUNDS: List[Dict[str, str]] = [
    {"code": "022364", "name": "永赢科技智选A"},
    {"code": "006502", "name": "财通集成电路产业股票A"},
    {"code": "018956", "name": "中航机遇领航混合发起A"},
]

# 你至少要跟踪 512810；另外两只按需保留/删除
ETFS: List[Dict[str, str]] = [
    {"code": "512810", "name": "国防军工ETF", "market": "sh"},  # 上交所
    {"code": "159399", "name": "现金流ETF",   "market": "sz"},  # 深交所（可删）
    {"code": "513530", "name": "港股红利ETF", "market": "sh"},  # 上交所（可删）
]

OUT_NAV_DIR = Path("reports/nav")
OUT_QT_DIR  = Path("reports/quotes")
OUT_NAV_DIR.mkdir(parents=True, exist_ok=True)
OUT_QT_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0"}

# ===== 小工具 =====
def bj_today_str() -> str:
    return (datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

def _retry_get(url: str, headers=None, timeout: int = 12, n: int = 3) -> requests.Response:
    last = None
    for i in range(1, n + 1):
        try:
            r = requests.get(url, headers=headers or UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            print(f"[retry {i}/{n}] GET {url} failed: {e}")
    raise last

def _read_existing_as_map(out_path: Path) -> "OrderedDict[str, List[str]]":
    mp: "OrderedDict[str, List[str]]" = OrderedDict()
    if not out_path.exists():
        return mp
    with open(out_path, "r", encoding="utf-8") as f:
        rdr = csv.reader(f)
        for i, row in enumerate(rdr):
            if i == 0:
                continue
            if len(row) >= 2:
                mp[row[1]] = row  # code -> row
    return mp

def write_csv_merged(rows: List[List[str]], out_path: Path, header: List[str]) -> None:
    existing = _read_existing_as_map(out_path)
    for r in rows:
        code = r[1]
        existing[code] = r  # 覆盖/新增
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in existing.values():
            w.writerow(row)

# ===== 抓基金净值 =====
def fetch_fund_last_nav(code: str) -> Tuple[str, float, float or str]:
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    r = _retry_get(url, headers=UA, timeout=12, n=3)
    r.encoding = "utf-8"
    txt = r.text

    m = re.search(r"Data_netWorthTrend\s*=\s*(\[[\s\S]*?\])\s*;", txt)
    if not m:
        key = "Data_netWorthTrend"
        idx = txt.find(key)
        if idx < 0:
            raise RuntimeError("no Data_netWorthTrend")
        start = txt.find("[", idx)
        end = txt.find("]", start) + 1
        arr_txt = txt[start:end]
    else:
        arr_txt = m.group(1)

    data = json.loads(arr_txt)
    if not data:
        raise RuntimeError("empty Data_netWorthTrend")

    last = data[-1]
    value = float(last.get("y"))
    date = datetime.fromtimestamp(int(last["x"]) / 1000).strftime("%Y-%m-%d")

    pct = last.get("equityReturn")
    if pct in (None, ""):
        if len(data) >= 2:
            prev_val = float(data[-2]["y"])
            pct = (value / prev_val - 1.0) * 100.0
        else:
            pct = ""

    return date, value, pct

# ===== 抓 ETF 行情（新浪） =====
def fetch_etf_quote_sina(market: str, code: str) -> Tuple[str, float, float]:
    symbol = f"{market}{code}"
    url = f"https://hq.sinajs.cn/list={symbol}"
    r = _retry_get(url, headers={"Referer": "https://finance.sina.com.cn/", **UA}, timeout=10, n=3)
    r.encoding = "gbk"
    txt = r.text.strip()
    # var hq_str_sh512810="国防军工ETF,0.713,0.718,0.712,0.719,0.710,...,2025-08-19,15:00:01,00";
    m = re.search(r'"([^"]+)"', txt)
    if not m:
        raise RuntimeError(f"sina quote parse error: {txt[:80]}")
    parts = m.group(1).split(",")
    if len(parts) < 4:
        raise RuntimeError(f"sina fields too short: {len(parts)}")
    last = float(parts[3])      # 当前/收盘价
    yclose = float(parts[2])    # 昨收
    date_str = parts[-3] if len(parts) >= 3 else bj_today_str()
    pct = (last / yclose - 1.0) * 100.0 if yclose > 0 else 0.0
    return date_str, last, pct

# ===== 主流程 =====
def run_once() -> None:
    # 1) 基金净值 -> reports/nav/<净值日>.csv
    fund_rows: List[List[str]] = []
    nav_dates = []
    for f in FUNDS:
        code = f["code"]; name = f["name"]
        try:
            print(f"[fund] {code} {name} ...")
            date, value, pct = fetch_fund_last_nav(code)
            print(f"       -> date={date}, value={value}, pct={pct}")
            nav_dates.append(date)
            fund_rows.append(["FUND", code, name, date, f"{value:.4f}", f"{pct:.2f}" if pct != "" else "", "eastmoney_api"])
        except Exception as e:
            print(f"[fund][ERROR] {code} {name}: {e}")
            fund_rows.append(["FUND", code, name, "", "", "fetch_error", "eastmoney_api"])

    nav_date = None
    if nav_dates:
        cnt = Counter([d for d in nav_dates if d])
        if cnt:
            nav_date = cnt.most_common(1)[0][0]
    if not nav_date:
        nav_date = bj_today_str()

    nav_path = OUT_NAV_DIR / f"{nav_date}.csv"
    write_csv_merged(fund_rows, nav_path, header=["type","code","name","date","value","pct","source"])
    print(f"[write] funds => {nav_path}")

    # 2) ETF 行情 -> reports/quotes/<交易日期>.csv
    qt_rows: List[List[str]] = []
    qt_dates = []
    for e in ETFS:
        code = e["code"]; name = e["name"]; mkt = e["market"]
        try:
            print(f"[etf]  {code} {name} ...")
            q_date, last, pct = fetch_etf_quote_sina(mkt, code)
            print(f"       -> date={q_date}, last={last}, pct={pct}")
            qt_dates.append(q_date)
            qt_rows.append(["ETF", code, name, q_date, f"{last:.3f}", f"{pct:.4f}", "sina_quote"])
        except Exception as e:
            print(f"[etf][ERROR] {code} {name}: {e}")
            qt_rows.append(["ETF", code, name, "", "", "fetch_error", "sina_quote"])

    qt_date = None
    if qt_dates:
        cnt2 = Counter([d for d in qt_dates if d])
        if cnt2:
            qt_date = cnt2.most_common(1)[0][0]
    if not qt_date:
        qt_date = bj_today_str()

    qt_path = OUT_QT_DIR / f"{qt_date}.csv"
    write_csv_merged(qt_rows, qt_path, header=["type","code","name","date","last","pct","source"])
    print(f"[write] etfs  => {qt_path}")

    # 预览两份文件
    def preview(p: Path, tag: str):
        try:
            print(f"===== preview {tag} =====")
            with open(p, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    sys.stdout.write(line)
                    if i > 20:
                        print("... (truncated)")
                        break
            print("========================")
        except Exception as e:
            print(f"[preview][ERROR] {tag}: {e}")

    preview(nav_path, "funds")
    preview(qt_path,  "etfs")

def main():
    run_once()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        traceback.print_exc()
        sys.exit(1)
