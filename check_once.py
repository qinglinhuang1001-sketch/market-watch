# -*- coding: utf-8 -*-
"""
check_once.py
盘中轮询一次：
- 读取 TOTAL_ASSETS 等配置（空/非法不崩）
- 监控 512810 及 022364/006502/018956
- 量能/回撤 触发买入提示（仅提示，不下单）
- Server酱推送（可选）
- 记录快照到 quotes/YYYY-MM-DD_HHMMSS.csv （避免冒号）
"""

import os
import time
import json
import math
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import csv

# ----------------------------
# 通用工具
# ----------------------------
def jp_now():
    return datetime.now(tz=timezone(timedelta(hours=9)))

def safe_filename_ts():
    # 避免冒号：用下划线和 HHMMSS
    return jp_now().strftime("%Y-%m-%d_%H%M%S")

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def get_total_assets(default="100000"):
    raw = os.getenv("TOTAL_ASSETS")
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        cleaned = str(raw).replace("_", "").replace(",", "").strip()
        return float(cleaned)
    except Exception:
        return float(default)

def notify_wechat(text: str, desp: str = ""):
    sckey = os.getenv("SCKEY", "").strip()
    if not sckey:
        return
    try:
        # Server酱新域名：sct.ftqq.com
        url = f"https://sctapi.ftqq.com/{sckey}.send"
        data = urllib.parse.urlencode({"title": text, "desp": desp}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()
    except Exception:
        pass

def http_get_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))

def http_get_text(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")

# ----------------------------
# 数据源（免费/简易）
# ----------------------------
def get_etf_sina_quote(symbol: str):
    """
    新浪 A 股/ETF 实时：返回 dict
    symbol 示例：'sh512810' 或 'sz159xxx'
    文档：返回逗号分隔字符串，字段很多，这里取常用：
    [0] 股票名称, [1] 今日开盘价, [2] 昨收, [3] 当前价, [8] 成交量(手), [9] 成交额(元)
    """
    url = f"https://hq.sinajs.cn/list={symbol}"
    txt = http_get_text(url, headers={"Referer": "https://finance.sina.com.cn"})
    # eg: var hq_str_sh512810="国防军工ETF,0.718,0.719,0.717,0.727,0.713,0.717,0.718,6830845,4903829.000, ...";
    if "=" not in txt or "\"" not in txt:
        return None
    payload = txt.split("=", 1)[1].strip().strip(";")
    payload = payload.strip("\"")
    parts = payload.split(",")
    if len(parts) < 10:
        return None
    name = parts[0]
    open_ = float(parts[1] or 0)
    preclose = float(parts[2] or 0)
    price = float(parts[3] or 0)
    high = float(parts[4] or 0)
    low = float(parts[5] or 0)
    volume_hand = float(parts[8] or 0)  # 手
    amount = float(parts[9] or 0)       # 元
    pct = 0.0
    if preclose > 0:
        pct = (price - preclose) / preclose * 100
    return {
        "name": name, "open": open_, "preclose": preclose, "price": price,
        "high": high, "low": low, "volume_hand": volume_hand, "amount": amount,
        "pct": pct
    }

def get_fund_estimate_eastmoney(code: str):
    """
    天天基金估值接口（JSONP），取估值涨跌和估算净值。
    示例：https://fundgz.1234567.com.cn/js/006002.js?rt=...
    返回：{"name":..., "gsz":估算净值, "gszzl":估算涨跌幅%}
    """
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    txt = http_get_text(url, headers={"Referer": "https://fund.eastmoney.com"})
    # jsonpgz({"fundcode":"006502","name":"财通集成电路产业股票A","gsz":"2.7722","gszzl":"-0.36","gztime":"2025-08-19 15:00"});
    if "jsonpgz(" not in txt:
        return None
    js = txt.strip().lstrip("jsonpgz(").rstrip(");")
    try:
        obj = json.loads(js)
        name = obj.get("name", "")
        gsz = float(obj.get("gsz") or 0.0)
        gszzl = float(obj.get("gszzl") or 0.0)  # %
        return {"name": name, "est_nav": gsz, "est_pct": gszzl}
    except Exception:
        return None

# ----------------------------
# 规则参数（可按需挪到 .env / Actions vars）
# ----------------------------
WATCH_ETF = "sh512810"       # 国防军工ETF
FUND_CODES = ["022364", "006502", "018956"]  # 022364 永赢科技智选A、006502 财通集成电路A、018956 中航机遇领航A

# ETF 波段：回撤阈值 & 量能突破
ETF_DIP_MIN = -8.0   # -8%
ETF_DIP_MAX = -5.0   # -5%
ETF_VOL_MULT = 1.5   # 成交量相较近 N 次均值的倍数
ETF_VOL_WINDOW = 20

# 基金买入：估值回撤阈值（你可按基金特性分开）
FUND_BUY_DIP = -2.0  # 例如估算跌到 -2% 附近，且在你的“预定区间”内

# ----------------------------
# 简单滑动窗口内存（Runner 生命周期内有效）
# ----------------------------
_VOL_CACHE = []  # 存最近 N 笔成交量（手）

def vol_breakout(current_hand: float, win: int = ETF_VOL_WINDOW, mult: float = ETF_VOL_MULT) -> bool:
    global _VOL_CACHE
    _VOL_CACHE.append(float(current_hand))
    if len(_VOL_CACHE) > win:
        _VOL_CACHE = _VOL_CACHE[-win:]
    if len(_VOL_CACHE) < win:
        return False
    base = sum(_VOL_CACHE[:-1]) / max(1, len(_VOL_CACHE)-1)
    return base > 0 and (current_hand >= base * mult)

# ----------------------------
# 主逻辑
# ----------------------------
def main():
    total_assets = get_total_assets("100000")
    pos_pct = float(os.getenv("POSITION_PCT", "0.10") or "0.10")   # 总资产 10% 为进攻仓上限
    atk_pct = float(os.getenv("ATTACK_PCT", "0.035") or "0.035")   # 单笔 3.5%（你希望 3~4%）
    atk_budget = total_assets * pos_pct * atk_pct

    # 1) ETF 实时
    etf = get_etf_sina_quote(WATCH_ETF)
    etf_signal = None
    if etf:
        dip = etf["pct"]  # 相对昨收的涨跌幅
        vol_ok = vol_breakout(etf["volume_hand"])
        dip_ok = (ETF_DIP_MIN <= dip <= ETF_DIP_MAX)  # 在 -8%~-5% 区间
        if dip_ok or vol_ok:
            etf_signal = {
                "type": "BUY",
                "code": "512810",
                "name": etf["name"],
                "reason": "vol_breakout" if vol_ok and not dip_ok else "dip_zone" if dip_ok else "vol+dip",
                "ref_price": etf["price"],
                "ref_pct": dip,
                "buy_suggest_cny": round(atk_budget, 2),
            }

    # 2) 场外基金估值
    fund_signals = []
    for code in FUND_CODES:
        info = get_fund_estimate_eastmoney(code)
        if not info:
            continue
        if info["est_pct"] <= FUND_BUY_DIP:
            fund_signals.append({
                "type": "BUY",
                "code": code,
                "name": info["name"],
                "reason": "est_dip",
                "ref_est_nav": info["est_nav"],
                "ref_est_pct": info["est_pct"],
                "buy_suggest_cny": round(total_assets * pos_pct * 0.10, 2),  # 例如进攻仓的 10% 用于单只基金
            })

    # 3) 输出快照 CSV（文件名无冒号）
    quotes_dir = Path("quotes")
    ensure_dir(quotes_dir)
    out_path = quotes_dir / f"{safe_filename_ts()}.csv"
    rows = []
    now_str = jp_now().strftime("%Y-%m-%d %H:%M:%S")
    if etf:
        rows.append(["ETF", "512810", etf["name"], now_str, etf["price"], etf["pct"], etf["volume_hand"]])
    for code in FUND_CODES:
        info = get_fund_estimate_eastmoney(code)
        if info:
            rows.append(["FUND", code, info["name"], now_str, info["est_nav"], info["est_pct"], "NA"])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type", "code", "name", "time", "value_or_nav", "pct_or_estpct", "volume_hand"])
        for r in rows:
            w.writerow(r)

    # 4) 通知（如有）
    messages = []
    if etf_signal:
        messages.append(f"BUY 512810 {etf_signal['name']} | {etf_signal['reason']} | 参考价:{etf_signal['ref_price']:.3f} | 回撤:{etf_signal['ref_pct']:.2f}% | 金额≈¥{int(etf_signal['buy_suggest_cny'])}")
    for s in fund_signals:
        messages.append(f"BUY {s['code']} {s['name']} | {s['reason']} | 估值:{s.get('ref_est_nav', 0):.4f} | 回撤:{s.get('ref_est_pct', 0):.2f}% | 金额≈¥{int(s['buy_suggest_cny'])}")

    if messages:
        title = messages[0][:45]
        body = "\n\n".join(messages) + f"\n\n快照: {out_path}"
        print("[ALERT]\n" + body)
        notify_wechat(title, body)
    else:
        print("no trigger. snapshot:", out_path)

if __name__ == "__main__":
    main()
