#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, re, csv, time, json, datetime as dt, requests
from typing import Optional, Dict

# ===== 跟踪标的 =====
INSTRUMENTS = [
    {"code": "022365", "name": "永赢科技智选C",  "type": "FUND"},
    {"code": "006502", "name": "财通集成电路A",  "type": "FUND"},
    {"code": "018956", "name": "中航机遇领航A","type": "FUND"},
    {"code": "018994", "name": "中欧数字经济A","type": "FUND"},
    {"code": "159399", "name": "现金流ETF",      "type": "ETF", "mq": "sz159399"},
    {"code": "513530", "name": "港股红利ETF",    "type": "ETF", "mq": "sh513530"},
    # 如需军工ETF：{"code":"512810","name":"国防军工ETF","type":"ETF","mq":"sh512810"},
]

REPORT_DIR = "reports/nav"

def bj_today():
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()

# ---------- 工具 ----------
UA = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 Chrome/123 Safari/537.36"}
def get(url, headers=None, retry=3, timeout=10, allow_status_error=False):
    headers = {**UA, **(headers or {})}
    last_err = None
    for _ in range(retry):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if not allow_status_error:
                r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            time.sleep(1.2)
    raise last_err

# ---------- FUND: 东财 F10 历史净值 最新一条 ----------
def fetch_fund_latest_nav(code: str) -> Optional[Dict]:
    url = f"http://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={code}&page=1&per=1"
    # 旧接口需要 Referer 才更稳
    try:
        r = get(url, headers={"Referer":"http://fundf10.eastmoney.com/"}, retry=3, timeout=10, allow_status_error=True)
        if r.status_code != 200:
            return None
        m = re.search(r"<table.*?>(.*?)</table>", r.text, re.S|re.I)
        if not m:
            return None
        rowm = re.search(r"<tr.*?>(.*?)</tr>", m.group(1), re.S|re.I)
        if not rowm:
            return None
        tds = re.findall(r"<td.*?>(.*?)</td>", rowm.group(1), re.S|re.I)
        if len(tds) < 4:
            return None
        date = (tds[0] or "").strip()
        unit = (tds[1] or "").strip()
        chg  = (tds[3] or "").strip().replace("%","")
        unit_val = float(unit) if unit not in ("", "--") else None
        chg_val  = float(chg)  if chg  not in ("", "--") else None
        return {"date": date, "nav": unit_val, "pct": chg_val}
    except Exception:
        return None

# ---------- ETF: 新浪行情 收盘价/涨跌幅 ----------
def fetch_etf_close(mq_code: str) -> Optional[Dict]:
    url = f"https://hq.sinajs.cn/list={mq_code}"
    try:
        r = get(url, headers={"Referer":"https://finance.sina.com.cn"}, retry=3, timeout=10, allow_status_error=True)
        if r.status_code != 200:
            return None
        r.encoding = "gbk"
        parts = r.text.split('="')[-1].strip('";\n').split(',')
        # 兜底解析
        name = parts[0] if len(parts) > 0 else mq_code
        prev = float(parts[2]) if len(parts) > 2 and parts[2] not in ("", "0") else None
        last = parts[3] if len(parts) > 3 else ""
        try:
            now = float(last) if last not in ("", "0") else None
        except Exception:
            now = None
        pct = round((now - prev) / prev * 100, 4) if (now is not None and prev not in (None, 0)) else None
        return {"date": str(bj_today()), "close": now, "pct": pct, "name": name}
    except Exception:
        return None

def ensure_dir(p): os.makedirs(p, exist_ok=True)

def main():
    ensure_dir(REPORT_DIR)
    outpath = os.path.join(REPORT_DIR, f"{bj_today()}.csv")
    rows = []
    for it in INSTRUMENTS:
        if it["type"] == "FUND":
            d = fetch_fund_latest_nav(it["code"])
            if d:
                rows.append([it["type"], it["code"], it["name"], d["date"], d["nav"], d["pct"], "eastmoney_f10"])
            else:
                rows.append([it["type"], it["code"], it["name"], "", "", "", "fetch_error"])
        else:
            d = fetch_etf_close(it["mq"])
            if d:
                rows.append([it["type"], it["code"], it["name"], d["date"], d["close"], d["pct"], "sina_quote"])
            else:
                rows.append([it["type"], it["code"], it["name"], "", "", "", "fetch_error"])
    # 一定写 CSV（即使部分失败）
    with open(outpath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type","code","name","date","value","pct","source"])
        w.writerows(rows)
    print("saved:", outpath)

if __name__ == "__main__":
    main()
