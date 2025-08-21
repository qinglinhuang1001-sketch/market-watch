# nav_fetch.py
# 夜间抓取基金/ETF的净值/行情，分组落地到 reports/nav/

import os, re, json, time, datetime
from pathlib import Path
import requests
import pandas as pd

# ---- 配置 ----
FUND_CODES = os.getenv("FUND_CODES", "022364,006502,018956").split(",")
ETF_CODES  = os.getenv("ETF_CODES",  "512810,513530,159399").split(",")

ROOT = Path(".")
OUT_DIR = ROOT / "reports" / "nav"
(OUT_DIR / "fund").mkdir(parents=True, exist_ok=True)
(OUT_DIR / "etf").mkdir(parents=True, exist_ok=True)

TODAY = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

def fetch_fund_nav_by_pingzhong(code: str):
    """从东财 pingzhongdata 获取历史净值，取最后一条"""
    url = f"http://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(time.time())}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    text = r.text
    # Data_netWorthTrend = [...]
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[[^\]]*\])", text)
    if not m:
        return None
    arr = json.loads(m.group(1))
    last = arr[-1]  # {'x': 日期毫秒, 'y': 净值, 'equityReturn': 涨跌幅(%)...}
    date = datetime.datetime.fromtimestamp(last["x"]/1000, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    return {"date": date, "value": float(last["y"]), "pct": float(last.get("equityReturn", 0))/100.0}

def fetch_fund_estimate(code: str):
    """东财基金估值接口（jsonp）jsonpgz({...})"""
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200 or "jsonpgz" not in r.text:
        return None
    m = re.search(r"jsonpgz\((\{.*\})\)", r.text)
    if not m:
        return None
    data = json.loads(m.group(1))
    # {'name','gsz'(估值),'gszzl'(估值涨跌幅 %),'gztime'}
    return {
        "date": data.get("gztime","")[:10],
        "value": float(data.get("gsz") or 0.0),
        "pct": float(data.get("gszzl") or 0.0)/100.0
    }

def code2sina_symbol(code: str) -> str:
    # 5/6 开头 -> sh； 0/1/3 -> sz；其余兜底 sz
    if code.startswith(("5","6")):
        return f"sh{code}"
    else:
        return f"sz{code}"

def fetch_etf_quote_sina(code: str):
    sym = code2sina_symbol(code)
    url = f"https://hq.sinajs.cn/list={sym}"
    r = requests.get(url, headers={"Referer":"https://finance.sina.com.cn","User-Agent":"Mozilla/5.0"}, timeout=10)
    r.raise_for_status()
    txt = r.text
    # var hq_str_sh510300="上证50,3.123,3.100,3.200,3.220,3.090,...,2025-08-19,15:00:03,00";
    parts = txt.split("=")[-1].strip('";\n').split(",")
    if len(parts) < 4 or parts[0] == "":
        return None
    name = parts[0]
    pre_close = float(parts[2] or 0.0)
    price = float(parts[3] or 0.0)
    pct = 0.0 if pre_close == 0 else (price - pre_close) / pre_close
    return {"name": name, "value": price, "pct": pct, "date": TODAY}

def main():
    records = []

    # 场外基金
    for code in FUND_CODES:
        code = code.strip()
        if not code:
            continue
        info = fetch_fund_nav_by_pingzhong(code) or fetch_fund_estimate(code)
        if info:
            name = f"Fund {code}"
            records.append(dict(type="FUND", code=code, name=name, date=info["date"], value=info["value"], pct=info["pct"], source="eastmoney_api"))
        else:
            records.append(dict(type="FUND", code=code, name=f"Fund {code}", date=TODAY, value=None, pct=None, source="fetch_error"))

    # ETF
    for code in ETF_CODES:
        code = code.strip()
        if not code:
            continue
        q = fetch_etf_quote_sina(code)
        if q:
            records.append(dict(type="ETF", code=code, name=q["name"], date=q["date"], value=q["value"], pct=q["pct"], source="sina_quote"))
        else:
            records.append(dict(type="ETF", code=code, name=f"ETF {code}", date=TODAY, value=None, pct=None, source="fetch_error"))

    df = pd.DataFrame(records)
    df["date"] = df["date"].fillna(TODAY)

    # 全量
    out_all = OUT_DIR / f"{TODAY}.csv"
    df.to_csv(out_all, index=False, encoding="utf-8-sig")

    # 分组
    df[df["type"]=="FUND"].to_csv(OUT_DIR/"fund"/f"{TODAY}.csv", index=False, encoding="utf-8-sig")
    df[df["type"]=="ETF"].to_csv(OUT_DIR/"etf"/f"{TODAY}.csv", index=False, encoding="utf-8-sig")

    print(f"[nav_fetch] done -> {out_all}")
    return 0

if __name__ == "__main__":
    main()
