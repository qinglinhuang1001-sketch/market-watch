#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, re, csv, datetime as dt, requests, json

# ===== 你要跟踪的 6 个标的 =====
# 4 只场外基金 + 2 只场内 ETF（如需增删，直接改这个列表）
INSTRUMENTS = [
    # 场外基金（官方净值）
    {"code": "022365", "name": "永赢科技智选C",    "type": "FUND"},
    {"code": "006502", "name": "财通集成电路A",    "type": "FUND"},
    {"code": "018956", "name": "中航机遇领航A",  "type": "FUND"},
    {"code": "018994", "name": "中欧数字经济A",  "type": "FUND"},
    # 场内 ETF（收盘价）
    {"code": "159399", "name": "现金流ETF",        "type": "ETF", "mq": "sz159399"},
    {"code": "513530", "name": "港股红利ETF",      "type": "ETF", "mq": "sh513530"},
    # 如需加军工ETF：{"code":"512810","name":"国防军工ETF","type":"ETF","mq":"sh512810"},
]

REPORT_DIR = "reports/nav"

def bj_today():
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()

# ---- FUND: 东财 F10 历史净值（最新一条） ----
# 旧接口，最稳定：返回 js 文本里带 <table>，我们用正则取第一行
def fetch_fund_latest_nav(code: str):
    url = f"http://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={code}&page=1&per=1"
    headers = {"Referer": f"http://fundf10.eastmoney.com/"}   # 需要一个 Referer 才稳
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    # 取出第一行 <tr> ... <td>日期</td><td>单位净值</td><td>累计净值</td><td>日涨跌幅</td>...
    m = re.search(r"<table.*?>(.*?)</table>", r.text, re.S|re.I)
    if not m:
        return None
    table = m.group(1)
    rowm = re.search(r"<tr.*?>(.*?)</tr>", table, re.S|re.I)
    if not rowm:
        return None
    tds = re.findall(r"<td.*?>(.*?)</td>", rowm.group(1), re.S|re.I)
    # 期望：日期/单位净值/累计净值/日涨跌幅/申购/赎回/分红
    date  = (tds[0] or "").strip()
    unit  = (tds[1] or "").strip()
    chg   = (tds[3] or "").strip().replace("%","")
    try:
        unit_val = float(unit)
    except:
        unit_val = None
    try:
        chg_val = float(chg)
    except:
        chg_val = None
    return {"date": date, "nav": unit_val, "pct": chg_val}

# ---- ETF: 新浪行情收盘价/涨跌幅 ----
def fetch_etf_close(mq_code: str):
    url = f"https://hq.sinajs.cn/list={mq_code}"
    headers = {"Referer":"https://finance.sina.com.cn","User-Agent":"Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    r.encoding = "gbk"
    parts = r.text.split('="')[-1].strip('";\n').split(',')
    # parts: [0]=名称, [2]=昨收, [3]=今开/现价?（收盘后为最新价）
    name = parts[0]
    now  = float(parts[3] or 0)
    prev = float(parts[2] or 0)
    pct  = round((now - prev) / prev * 100, 4) if prev else None
    return {"date": str(bj_today()), "close": now, "pct": pct, "name": name}

def ensure_dir(path): os.makedirs(path, exist_ok=True)

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
            d = fetch_etf_close(it["mq"])
            rows.append([it["type"], it["code"], it["name"], d["date"], d["close"], d["pct"], "sina_quote"])
    # 写 CSV
    with open(outpath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type","code","name","date","value","pct","source"])
        w.writerows(rows)
    print("saved:", outpath)

if __name__ == "__main__":
    main()
