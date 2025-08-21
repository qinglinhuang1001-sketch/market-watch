# -*- coding: utf-8 -*-
import os
import re
import csv
import json
import time
import math
import glob
import random
import urllib.parse
import datetime as dt
from pathlib import Path

import requests

# -----------------------------
# 基础配置（可按需改）
# -----------------------------
CONFIG = {
    "total_assets": float(os.getenv("TOTAL_ASSETS", "100000.0")),  # 总资产，缺省 10 万
    "offense_pct": 0.10,     # 进攻仓 = 总资产的 10%
    "etf_trade_pct_range": (0.03, 0.04),  # 单只 ETF 下单 = 进攻仓的 3%~4%
    "signal_tag": "auto",    # 信号标签
    "guard_scale": 0.30,     # 口径防错闸：与最近净值偏差>30% 丢弃
}

# 标的清单（可迁到 yaml/json；为便于落地，直接内嵌）
SYMBOLS = {
    "funds": [
        {"code": "022364", "name": "永赢科技智选A"},
        {"code": "006502", "name": "财通集成电路产业股票A"},
        {"code": "018956", "name": "中航机遇领航混合发起A"},
    ],
    "etfs": [
        {"code": "512810", "name": "国防军工ETF", "market": "sh",
         "pullback_window": (-8.0, -5.0),  # 从当日高点回撤区间（负值）
         "vol_breakout": {"ma": 5, "factor": 1.8}},   # 放量突破阈值
    ],
}

SCT_KEY = os.getenv("SCT_KEY", "").strip()  # 方糖气球 SCTKEY

# -----------------------------
# 工具
# -----------------------------
ROOT = Path(".").resolve()
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
(REPORTS_DIR / "nav").mkdir(parents=True, exist_ok=True)
(REPORTS_DIR / "quotes").mkdir(parents=True, exist_ok=True)

def jst_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=9)

def today_str():
    return jst_now().strftime("%Y-%m-%d")

def send_wechat(title, content):
    """Server 酱推送；需要 secrets: SCT_KEY"""
    if not SCT_KEY:
        print("[WARN] SCT_KEY missing, skip push:", title)
        return
    url = f"https://sctapi.ftqq.com/{SCT_KEY}.send"
    data = {"title": title, "desp": content}
    try:
        r = requests.post(url, data=data, timeout=10)
        print("[SCT] status:", r.status_code, r.text[:200])
    except Exception as e:
        print("[SCT] push error:", repr(e))

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def guard_price_scale(new_price, last_nav, guard=CONFIG["guard_scale"]):
    """口径防错闸：仅对 FUND 使用"""
    if last_nav is None:
        return True
    if last_nav <= 0 or new_price is None or new_price <= 0:
        return True
    diff = abs(new_price - last_nav) / last_nav
    if diff > guard:
        return False
    return True

# -----------------------------
# FUND：净值/估值读取
# -----------------------------
def latest_nav_from_reports(code):
    """
    从 reports/nav/*.csv 读取最近日期的净值记录：
    CSV 字段示例：type,code,name,date,value,pct,source
    """
    files = sorted((REPORTS_DIR / "nav").glob("*.csv"))
    latest_row = None
    for fp in reversed(files):
        with open(fp, "r", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                if row.get("code") == code:
                    latest_row = row
                    break
        if latest_row:
            break
    if latest_row:
        try:
            return {
                "date": latest_row.get("date"),
                "nav": float(latest_row.get("value")),
                "pct": float(latest_row.get("pct")) if latest_row.get("pct") else None,
                "source": latest_row.get("source"),
            }
        except:
            pass
    return None

def eastmoney_fund_gz(code):
    """
    东财基金估值接口（仅观察用，不用于买卖口径）：
    http://fundgz.1234567.com.cn/js/006502.js?rt=timestamp
    返回：{name, gztime, gszzl(估算涨幅%), gszz(估算净值)}
    """
    url = f"http://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200 and "jsonpgz" in r.text:
            # jsonpgz({"fundcode":"006502","name":"xxx","gszz":"2.77","gszzl":"-0.36","gztime":"2025-08-20 15:00"});
            m = re.search(r"jsonpgz\((\{.*?\})\);", r.text)
            if m:
                obj = json.loads(m.group(1))
                return {
                    "name": obj.get("name"),
                    "est": float(obj.get("gszz")) if obj.get("gszz") else None,
                    "est_pct": float(obj.get("gszzl")) if obj.get("gszzl") else None,
                    "time": obj.get("gztime"),
                    "source": "eastmoney_est",
                }
    except Exception as e:
        print("[EM gz] error", code, repr(e))
    return None

# -----------------------------
# ETF：新浪实时行情 + 成交量历史
# -----------------------------
def fetch_sina_quote(market, code):
    """
    新浪简易行情：返回当日 open, preclose, now, high, low, volume(手), amount(元)
    api: https://hq.sinajs.cn/list=sh512810
    """
    mkt_code = f"{market}{code}"
    url = f"https://hq.sinajs.cn/list={mkt_code}"
    try:
        r = requests.get(url, timeout=6)
        r.encoding = "gbk"
        if r.status_code != 200:
            return None
        # v_sh512810="国防军工ETF,0.713,0.712,0.713,0.72,0.711, ... ,成交量(手),成交额,日期,时间,";
        parts = r.text.split("=")
        raw = parts[1].strip('";\n ')
        arr = raw.split(",")
        if len(arr) < 32:
            return None
        name = arr[0]
        open_p = float(arr[1]) if arr[1] else None
        preclose = float(arr[2]) if arr[2] else None
        now_p = float(arr[3]) if arr[3] else None
        high = float(arr[4]) if arr[4] else None
        low = float(arr[5]) if arr[5] else None
        vol_hand = float(arr[8]) if arr[8] else 0.0  # 手
        amount = float(arr[9]) if arr[9] else 0.0
        date = arr[-3]
        tstr = arr[-2]
        return {
            "code": code, "market": market, "name": name,
            "now": now_p, "open": open_p, "preclose": preclose,
            "high": high, "low": low,
            "vol": vol_hand * 100.0,   # 股
            "amount": amount,
            "date": date, "time": tstr,
            "source": "sina_quote"
        }
    except Exception as e:
        print("[SINA] error", code, repr(e))
    return None

VOL_FILE = DATA_DIR / "etf_vol_hist.csv"

def append_etf_volume(code, date, vol):
    VOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    exists = VOL_FILE.exists()
    with open(VOL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["date", "code", "vol"])
        w.writerow([date, code, int(vol or 0)])

def etf_vol_ma(code, ma=5):
    if not VOL_FILE.exists():
        return None
    rows = []
    with open(VOL_FILE, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for row in rd:
            if row["code"] == code:
                rows.append((row["date"], int(row["vol"])))
    rows.sort(key=lambda x: x[0])
    rows = rows[-ma:]
    if not rows:
        return None
    return sum(v for _, v in rows) / len(rows)

# -----------------------------
# 金额引擎
# -----------------------------
def offense_amount(total):
    return total * CONFIG["offense_pct"]

def etf_amount_range(total):
    off = offense_amount(total)
    a = off * CONFIG["etf_trade_pct_range"][0]
    b = off * CONFIG["etf_trade_pct_range"][1]
    return (round(a, 2), round(b, 2))

def fund_split_amount(total, n):
    off = offense_amount(total)
    if n <= 0:
        return 0.0
    return round(off / n, 2)

# -----------------------------
# 去重日志（当天只提醒一次）
# -----------------------------
def has_sent(key):
    path = LOG_DIR / f"signals-{today_str()}.json"
    data = read_json(path, {"sent": []})
    return key in data.get("sent", [])

def mark_sent(key):
    path = LOG_DIR / f"signals-{today_str()}.json"
    data = read_json(path, {"sent": []})
    if key not in data["sent"]:
        data["sent"].append(key)
        write_json(path, data)

# -----------------------------
# 触发逻辑
# -----------------------------
def check_funds():
    """基金（场外）：只看净值/估值；触发=净值回撤窗口/或催化（此处先给净值回撤示例）"""
    triggered = []
    # 读取最后一次净值（从 reports/nav）
    meta_list = []
    for f in SYMBOLS["funds"]:
        code = f["code"]
        last = latest_nav_from_reports(code)
        est = eastmoney_fund_gz(code)  # 估值，只做观察
        meta_list.append({"code": code, "name": f["name"], "last": last, "est": est})

    # 这里给一个“净值回撤窗口示例”：最近一次 pct <= -1% 即触发（可根据你的窗口改）
    for m in meta_list:
        code, name = m["code"], m["name"]
        last = m["last"]
        if not last:
            continue
        pct = last.get("pct")
        nav = last.get("nav")
        # 口径防错闸：估值（若有）与净值偏差>30%，不触发买卖，只发观察
        if m["est"] and not guard_price_scale(m["est"]["est"], nav):
            title = f"[OBSERVE] {code} {name} 估值口径异常"
            msg = f"最近净值: {nav}({last.get('date')})；估值: {m['est']['est']}({m['est']['time']})；偏差过大，口径不一致，仅观察。"
            send_wechat(title, msg)
            continue

        if pct is not None and pct <= -1.0:  # 示例阈值（你也可以改为你的区间）
            triggered.append({"code": code, "name": name, "nav": nav, "pct": pct, "date": last["date"]})

    # 金额建议：平分进攻仓
    if triggered:
        amt = fund_split_amount(CONFIG["total_assets"], len(triggered))
        for t in triggered:
            key = f"FUND-{t['code']}-{today_str()}"
            if has_sent(key):
                continue
            title = f"BUY {t['code']} {t['name']}（FUND）"
            msg = f"净值回撤触发：{t['pct']}%，净值={t['nav']}（{t['date']}）。\n建议额度≈¥{amt}（按总资产10%进攻仓平分）。"
            send_wechat(title, msg)
            mark_sent(key)

def check_etfs():
    """ETF：回撤区间 or 放量突破"""
    for e in SYMBOLS["etfs"]:
        code, name, market = e["code"], e["name"], e["market"]
        q = fetch_sina_quote(market, code)
        if not q or not q.get("now") or not q.get("high"):
            print("[ETF] quote missing", code)
            continue

        # 记录今日成交量到历史表
        append_etf_volume(code, today_str(), q["vol"])
        vol_ma = etf_vol_ma(code, e["vol_breakout"]["ma"])

        # 回撤：当前价相对今日高点的回撤
        drawdown = (q["now"] - q["high"]) / q["high"] * 100.0 if q["high"] else None
        pull_low, pull_high = e["pullback_window"]
        cond_pull = (drawdown is not None) and (pull_low <= drawdown <= pull_high)  # drawdown 为负值

        # 放量突破：当前累积量 > ma * factor
        cond_vol = False
        if vol_ma and vol_ma > 0:
            cond_vol = q["vol"] > vol_ma * e["vol_breakout"]["factor"]

        print(f"[ETF] {code} now={q['now']} high={q['high']} drawdown={drawdown:.2f}% vol={q['vol']:.0f} ma={vol_ma} cond_pull={cond_pull} cond_vol={cond_vol}")

        if not (cond_pull or cond_vol):
            continue

        # 金额建议
        lo, hi = etf_amount_range(CONFIG["total_assets"])
        key = f"ETF-{code}-{today_str()}"
        if has_sent(key):
            continue

        title = f"BUY {code} {name}（ETF）"
        detail = []
        if cond_pull:
            detail.append(f"回撤落入区间[{pull_low}%, {pull_high}%]，当前回撤≈{drawdown:.2f}%")
        if cond_vol:
            detail.append(f"放量突破：vol≈{int(q['vol'])} > {e['vol_breakout']['factor']}×MA{e['vol_breakout']['ma']}≈{int(vol_ma or 0)}")
        detail = "；".join(detail)

        msg = (
            f"{detail}\n"
            f"现价≈{q['now']}，高点≈{q['high']}，昨收≈{q['preclose']}。\n"
            f"建议下单额度：¥{lo} ~ ¥{hi}（进攻仓10%的3%~4%）。\n"
            f"数据源：新浪。时间：{q['date']} {q['time']}。"
        )
        send_wechat(title, msg)
        mark_sent(key)

# -----------------------------
# MAIN
# -----------------------------
def main():
    print("=== check_once.py start ===")
    print("TODAY:", today_str())
    print("TOTAL_ASSETS:", CONFIG["total_assets"])

    # FUND：净值/估值口径，不跑盘中量价
    check_funds()

    # ETF：盘中量价
    check_etfs()

    print("=== check_once.py done ===")

if __name__ == "__main__":
    main()
