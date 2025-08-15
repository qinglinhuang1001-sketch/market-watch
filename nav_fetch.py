#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
稳定抓取：基金官方净值 + ETF 收盘价
- 三层兜底：fundmobapi(JSON) -> api.fund.eastmoney(JSON) -> F10 HTML
- 逐标的 try/except（任何失败都不让脚本退出）
- 一定会生成 reports/nav/YYYY-MM-DD.csv；失败行写 fetch_error
- 退出码固定 0，避免 GitHub Actions 因单标的失败而 fail
"""

import os, re, csv, time, json, sys, datetime as dt, requests
from typing import Optional, Dict

# ===== 跟踪列表：可按需增删 =====
INSTRUMENTS = [
    # —— 场外基金（官方净值）——
    {"code": "022364", "name": "永赢科技智选A",  "type": "FUND"},
    {"code": "022365", "name": "永赢科技智选C",  "type": "FUND"},   # 如不需要C，删掉即可
    {"code": "006502", "name": "财通集成电路A",  "type": "FUND"},
    {"code": "018956", "name": "中航机遇领航A","type": "FUND"},
    {"code": "018994", "name": "中欧数字经济A","type": "FUND"},

    # —— 场内 ETF（收盘价）——
    {"code": "159399", "name": "现金流ETF",      "type": "ETF", "mq": "sz159399"},
    {"code": "513530", "name": "港股红利ETF",    "type": "ETF", "mq": "sh513530"},
    {"code": "512810", "name": "国防军工ETF",    "type": "ETF", "mq": "sh512810"},  # 你新增的军工
]

REPORT_DIR = "reports/nav"

def bj_today_date():
    """北京当日 date 对象"""
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()

def today_str():
    return str(bj_today_date())

# ---- UA / 请求工具 ----
UA_PC  = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 Chrome/123 Safari/537.36"}
UA_MOB = {"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 EFund/6.5.9"}

def safe_float(x):
    if x is None: return None
    s = str(x).strip().replace("%","")
    if s in ("", "--"): return None
    try:
        return float(s)
    except Exception:
        return None

# ---------- 方案A：东财移动端 JSON（最稳） ----------
def fetch_fund_latest_nav_mob(code: str) -> Optional[Dict]:
    url = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetListNew"
           f"?FCODE={code}&pageIndex=1&pageSize=1&IsShareNet=1"
           "&appType=ttjj&plat=Iphone&product=EFund&version=6.5.9")
    try:
        r = requests.get(url, headers=UA_MOB, timeout=12)
        if r.status_code != 200:
            return None
        j = r.json()
        datas = j.get("Datas") or []
        if not datas:
            return None
        d = datas[0]
        return {
            "date": d.get("PDATE"),
            "nav":  safe_float(d.get("NAV")),
            "pct":  safe_float(d.get("NAVCHGRT")),  # 百分比数值，如 0.85
            "source": "fundmobapi"
        }
    except Exception:
        return None

# ---------- 方案B：api.fund.eastmoney JSON ----------
def fetch_fund_latest_nav_api(code: str) -> Optional[Dict]:
    url = f"http://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=1&startDate=&endDate="
    hdr = {**UA_PC, "Referer":"http://fundf10.eastmoney.com/"}
    try:
        r = requests.get(url, headers=hdr, timeout=12)
        if r.status_code != 200:
            return None
        j = r.json()
        rows = (j.get("Data") or {}).get("LSJZList") or []
        if not rows: return None
        d = rows[0]
        return {
            "date": d.get("FSRQ"),
            "nav":  safe_float(d.get("DWJZ")),
            "pct":  safe_float(d.get("JZZZL")),
            "source": "eastmoney_api"
        }
    except Exception:
        return None

# ---------- 方案C：旧 HTML 表格兜底 ----------
def fetch_fund_latest_nav_html(code: str) -> Optional[Dict]:
    url = f"http://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={code}&page=1&per=1"
    hdr = {**UA_PC, "Referer":"http://fundf10.eastmoney.com/"}
    try:
        r = requests.get(url, headers=hdr, timeout=12)
        if r.status_code != 200:
            return None
        m = re.search(r"<table.*?>(.*?)</table>", r.text, re.S|re.I)
        if not m: return None
        rowm = re.search(r"<tr.*?>(.*?)</tr>", m.group(1), re.S|re.I)
        if not rowm: return None
        tds = re.findall(r"<td.*?>(.*?)</td>", rowm.group(1), re.S|re.I)
        if len(tds) < 4: return None
        return {
            "date": (tds[0] or "").strip(),
            "nav":  safe_float((tds[1] or "").strip()),
            "pct":  safe_float((tds[3] or "").strip()),
            "source": "f10_html"
        }
    except Exception:
        return None

def fetch_fund_latest_nav(code: str) -> Optional[Dict]:
    return (fetch_fund_latest_nav_mob(code)
            or fetch_fund_latest_nav_api(code)
            or fetch_fund_latest_nav_html(code))

# ---------- ETF：新浪行情 ----------
def fetch_etf_close(mq_code: str) -> Optional[Dict]:
    url = f"https://hq.sinajs.cn/list={mq_code}"
    hdr = {**UA_PC, "Referer":"https://finance.sina.com.cn"}
    try:
        r = requests.get(url, headers=hdr, timeout=10)
        r.encoding = "gbk"
        parts = r.text.split('="')[-1].strip('";\n').split(',')
        name = parts[0] if len(parts)>0 else mq_code
        prev = safe_float(parts[2] if len(parts)>2 else None)
        now  = safe_float(parts[3] if len(parts)>3 else None)
        pct  = round((now - prev)/prev*100, 4) if (now is not None and prev not in (None,0)) else None
        return {"date": today_str(), "close": now, "pct": pct, "name": name, "source": "sina_quote"}
    except Exception:
        return None

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def main():
    ensure_dir(REPORT_DIR)
    outpath = os.path.join(REPORT_DIR, f"{today_str()}.csv")

    rows = []
    for it in INSTRUMENTS:
        try:
            if it["type"] == "FUND":
                d = fetch_fund_latest_nav(it["code"])
                if d:
                    rows.append(["FUND", it["code"], it["name"], d["date"], d["nav"], d["pct"], d["source"]])
                else:
                    rows.append(["FUND", it["code"], it["name"], "", "", "", "fetch_error"])
            else:
                d = fetch_etf_close(it["mq"])
                if d:
                    rows.append(["ETF", it["code"], it["name"], d["date"], d["close"], d["pct"], d["source"]])
                else:
                    rows.append(["ETF", it["code"], it["name"], "", "", "", "fetch_error"])
        except Exception as e:
            rows.append([it["type"], it["code"], it["name"], "", "", "", f"error:{type(e).__name__}"])

    with open(outpath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type","code","name","date","value","pct","source"])
        w.writerows(rows)

    print("saved:", outpath)
    sys.exit(0)  # 永远退出 0，避免 Actions 标红

if __name__ == "__main__":
    main()
