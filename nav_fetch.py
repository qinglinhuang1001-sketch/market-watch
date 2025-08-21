# -*- coding: utf-8 -*-
"""
nav_fetch.py
抓取：
1) 场外基金最新净值（东财） -> reports/nav/YYYY-MM-DD.csv
2) ETF 场内行情（新浪）   -> reports/quotes/YYYY-MM-DD.csv

字段统一：
- 基金净值：type,code,name,date,value,pct,source
- ETF 行情：type,code,name,date,last,pct,source

作者：你的量化小助手
"""

import csv
import os
import time
from datetime import datetime, timezone, timedelta

import requests

# -------------------------
# 配置：跟踪标的
# -------------------------
FUNDS = [
    {"code": "022364", "name": "永赢科技智选A"},
    {"code": "006502", "name": "财通集成电路产业股票A"},
    {"code": "018956", "name": "中航机遇领航混合发起A"},
]

# 仅保留你需要的 ETF；market 必选：上交所 sh / 深交所 sz
ETFS = [
    {"code": "512810", "name": "国防军工ETF", "market": "sh"},
    # {"code": "159399", "name": "现金流ETF", "market": "sz"},
    # {"code": "513530", "name": "港股红利ETF", "market": "sh"},
]

# 时区：日本（与用户一致）
JST = timezone(timedelta(hours=9))


# -------------------------
# 工具函数
# -------------------------
def today_str_jst():
    return datetime.now(JST).strftime("%Y-%m-%d")


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def http_get(url, params=None, headers=None, retries=3, timeout=8):
    """
    简单 GET with retry
    """
    headers = headers or {}
    last_err = None
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            # 东财接口需要 200 且有 JSON
            if resp.status_code == 200:
                return resp
            last_err = Exception(f"HTTP {resp.status_code}")
        except Exception as e:
            last_err = e
        time.sleep(1 + i)
    raise last_err


# -------------------------
# 基金（东财）抓取
# -------------------------
def fetch_fund_latest_from_eastmoney(code: str):
    """
    通过东财历史净值接口取最新一条
    接口： https://api.fund.eastmoney.com/f10/lsjz?fundCode=006502&pageIndex=1&pageSize=1
    返回 JSON 中的 LSJZList 第一条：
      FSRQ: 日期
      DWJZ: 单位净值（str）
      JZZZL: 当日涨跌幅 %（str，可能为空）
    """
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {"fundCode": code, "pageIndex": 1, "pageSize": 1}
    headers = {
        # 东财接口通常需要一个 Referer 才返回 JSON
        "Referer": "https://fundf10.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        r = http_get(url, params=params, headers=headers)
        data = r.json()
        # 结构容错
        if not data or "Data" not in data or "LSJZList" not in data["Data"]:
            return None
        lst = data["Data"]["LSJZList"]
        if not lst:
            return None
        row = lst[0]
        date = row.get("FSRQ")  # 'YYYY-MM-DD'
        value = row.get("DWJZ")  # 单位净值 str
        pct = row.get("JZZZL", "")  # 当日涨跌幅 % str 可能为 ''
        # 统一格式：value->float, pct->float(百分数)
        try:
            value = float(value)
        except Exception:
            value = None
        try:
            pct = float(pct)
        except Exception:
            pct = None
        return {"date": date, "value": value, "pct": pct, "source": "eastmoney_api"}
    except Exception:
        return None


def build_fund_rows():
    rows = []
    for f in FUNDS:
        code, name = f["code"], f["name"]
        item = fetch_fund_latest_from_eastmoney(code)
        if item:
            rows.append(
                {
                    "type": "FUND",
                    "code": code,
                    "name": name,
                    "date": item["date"],
                    "value": item["value"],
                    "pct": item["pct"],
                    "source": item["source"],
                }
            )
        else:
            # 拉取失败也保留一行，便于定位
            rows.append(
                {
                    "type": "FUND",
                    "code": code,
                    "name": name,
                    "date": today_str_jst(),
                    "value": None,
                    "pct": None,
                    "source": "fetch_error",
                }
            )
    return rows


def save_fund_nav(rows):
    ensure_dir("reports/nav")
    # 以“最新一只基金的 date”为文件名；如果都失败，则用今天
    dates = [r["date"] for r in rows if r.get("date")]
    file_date = dates[0] if dates else today_str_jst()
    path = f"reports/nav/{file_date}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["type", "code", "name", "date", "value", "pct", "source"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"[FUND] saved -> {path}")


# -------------------------
# ETF（新浪）抓取
# -------------------------
def fetch_sina_quote(market: str, code: str):
    """
    新浪接口： https://hq.sinajs.cn/list=sh512810
    返回：var hq_str_sh512810="国防军工,0.713,0.714,0.712,...";
    字段含义见文档，这里取：
      name: fields[0]
      open: fields[1]
      preclose: fields[2]
      last: fields[3]  (实时/最新价)
      ...
    pct = (last - preclose) / preclose * 100
    """
    url = f"https://hq.sinajs.cn/list={market}{code}"
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    try:
        r = http_get(url, headers=headers)
        text = r.text.strip()
        # 容错
        if "=" not in text or "," not in text:
            return None
        # 解析
        # var hq_str_sh512810="国防军工,0.713,0.714,0.712,0.713,...";
        info = text.split("=", 1)[1].strip().strip('";')
        fields = info.split(",")
        if len(fields) < 4:
            return None
        name = fields[0]
        try:
            last = float(fields[3])
        except Exception:
            last = None
        try:
            preclose = float(fields[2])
            pct = None if preclose == 0 else (last - preclose) / preclose * 100
        except Exception:
            pct = None
        return {
            "name": name if name else code,
            "last": last,
            "pct": pct,
            "date": today_str_jst(),
            "source": "sina_quote",
        }
    except Exception:
        return None


# -------------------------
# ETF（新浪）抓取（加粗版）
# -------------------------
def build_etf_rows_safe():
    rows = []
    print("[ETF ] start building etf rows, targets =", ETFS)
    try:
        for e in ETFS:
            code, name, market = e["code"], e["name"], e["market"]
            item = fetch_sina_quote(market, code)
            if item:
                rows.append(
                    {
                        "type": "ETF",
                        "code": code,
                        "name": name or item["name"],
                        "date": item["date"],
                        "last": item["last"],
                        "pct": item["pct"],
                        "source": item["source"],
                    }
                )
            else:
                rows.append(
                    {
                        "type": "ETF",
                        "code": code,
                        "name": name,
                        "date": today_str_jst(),
                        "last": None,
                        "pct": None,
                        "source": "fetch_error",
                    }
                )
    except Exception as e:
        # 不让异常中断；写一行提示
        print("[ETF ] build_etf_rows_safe error:", repr(e))
    print("[ETF ] built rows num =", len(rows))
    return rows


def save_etf_quotes_force(rows):
    ensure_dir("reports/quotes")
    # 再保险：即便 rows 为空，也要写一个今日空表（至少创建目录 + 文件）
    dates = [r.get("date") for r in rows if r.get("date")]
    file_date = dates[0] if dates else today_str_jst()
    path = f"reports/quotes/{file_date}.csv"
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["type", "code", "name", "date", "last", "pct", "source"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"[ETF ] saved -> {path} (rows={len(rows)})")
    except Exception as e:
        print("[ETF ] save_etf_quotes_force error:", repr(e))


# -------------------------
# Main
# -------------------------
def main():
    # 基金净值
    fund_rows = build_fund_rows()
    save_fund_nav(fund_rows)

    # ETF 行情（强制落地）
    etf_rows = build_etf_rows_safe()
    save_etf_quotes_force(etf_rows)

if __name__ == "__main__":
    main()
