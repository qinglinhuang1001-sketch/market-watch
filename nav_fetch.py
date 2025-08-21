#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
收盘后抓取三只基金的最新净值，写入 reports/nav/<净值日期>.csv
- 数据源：fund.eastmoney.com/pingzhongdata/<code>.js
- 稳定性：请求重试、正则提取 Data_netWorthTrend
- 兼容：equityReturn 缺失时以前一日净值计算 pct
- 命名：文件名使用“净值日期”，避免日期错位
- 幂等：同一日期多轮抓取会合并更新（不丢已有成功记录）
- 输出列：type, code, name, date, value, pct, source
"""

import os
import re
import csv
import json
import requests
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Tuple, List, Dict

# ===== 配置 =====
FUNDS: List[Dict[str, str]] = [
    {"code": "022364", "name": "永赢科技智选A"},
    {"code": "006502", "name": "财通集成电路产业股票A"},
    {"code": "018956", "name": "中航机遇领航混合发起A"},
]
OUT_DIR = Path("reports/nav")
OUT_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0"}

# ===== 工具函数 =====
def bj_today_str() -> str:
    # 北京时间
    return (datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

def _retry_get(url: str, headers=None, timeout: int = 12, n: int = 3) -> requests.Response:
    last = None
    for _ in range(n):
        try:
            r = requests.get(url, headers=headers or UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
    # 最后一次仍失败则抛出
    raise last

def fetch_fund_last_nav(code: str) -> Tuple[str, float, float or str]:
    """
    抓取单只基金的“最新一条净值”
    返回: (date_str, value_float, pct_float或"")
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    r = _retry_get(url, headers=UA, timeout=12, n=3)
    r.encoding = "utf-8"
    txt = r.text

    # 更稳：正则提取 Data_netWorthTrend 数组
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[[\s\S]*?\])\s*;", txt)
    if not m:
        # 兜底：旧方式
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
    # 日期使用净值自带时间戳
    date = datetime.fromtimestamp(int(last["x"]) / 1000).strftime("%Y-%m-%d")

    # 当日涨跌幅：优先 equityReturn；无则用前一日净值计算
    pct = last.get("equityReturn")
    if pct in (None, ""):
        if len(data) >= 2:
            prev_val = float(data[-2]["y"])
            pct = (value / prev_val - 1.0) * 100.0
        else:
            pct = ""

    return date, value, pct

def _read_existing_as_map(out_path: Path) -> "OrderedDict[str, List[str]]":
    """
    读取已有 CSV（若存在），返回 code -> row 的顺序字典
    """
    mp: "OrderedDict[str, List[str]]" = OrderedDict()
    if not out_path.exists():
        return mp
    with open(out_path, "r", encoding="utf-8") as f:
        rdr = csv.reader(f)
        for i, row in enumerate(rdr):
            if i == 0:
                continue  # 跳过表头
            if len(row) >= 2:
                mp[row[1]] = row  # code -> row
    return mp

def write_csv_merged(rows: List[List[str]], out_path: Path) -> None:
    """
    幂等写入：将新 rows 与已存在的同日文件按 code 合并后写回
    """
    header = ["type", "code", "name", "date", "value", "pct", "source"]
    existing = _read_existing_as_map(out_path)
    for r in rows:
        code = r[1]
        existing[code] = r  # 覆盖/新增

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in existing.values():
            w.writerow(row)

# ===== 主流程 =====
def main():
    rows: List[List[str]] = []
    nav_dates = set()

    for f in FUNDS:
        code = f["code"]; name = f["name"]
        try:
            date, value, pct = fetch_fund_last_nav(code)
            nav_dates.add(date)
            rows.append(["FUND", code, name, date, f"{value:.4f}", f"{pct:.2f}" if pct != "" else "", "eastmoney_api"])
        except Exception as e:
            # 失败也写入一行，便于排查；date 暂空
            rows.append(["FUND", code, name, "", "", "fetch_error", "eastmoney_api"])

    # 选定文件名用的日期：
    # - 若三只基金返回的净值日期一致：用该日期
    # - 否则：用北京“今天”（避免同日多文件）
    nav_dates_nonempty = [d for d in nav_dates if d]
    if len(nav_dates_nonempty) == 1:
        nav_date = nav_dates_nonempty[0]
    else:
        nav_date = bj_today_str()

    out_path = OUT_DIR / f"{nav_date}.csv"
    write_csv_merged(rows, out_path)
    print(f"[nav_fetch] wrote: {out_path}")

if __name__ == "__main__":
    main()
