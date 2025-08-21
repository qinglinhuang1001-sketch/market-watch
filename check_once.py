#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中监控（单次轮询）：
- 主动基金：022364 / 006502 / 018956 —— 用行业ETF做“价格+量能”代理信号
- ETF：512810 —— 直接用自身“价格+量能”
买点：
  A) 回撤买点：相对滚动20日高回撤 in [5%, 8%]
  B) 放量突破：当前价 > 20日高，且“盘中成交量” >= 1.8 × 近20日日均量 × 时间进度
风控：
  - 卖出提醒统一由夜间复盘或事件（阅兵后一周）处理；盘中只做买点提示
执行窗：
  - 10:00–14:45（上海时区）；同标的当日仅提醒一次
输出：
  - logs/signals.csv 追加（若日志不存在会自动创建）
可选通知：
  - Server酱：环境变量 SERVER_CHAN_KEY
依赖：
  - requests
"""

import os, csv, time, math, json, requests
from datetime import datetime, timedelta, timezone

# ====== 配置区（按需改） =======================================================

# 总资产（用于资金测算）；也可从环境变量 TOTAL_ASSET 传入
TOTAL_ASSET = float(os.getenv("TOTAL_ASSET", "100000"))

# 进攻仓（主动基金合计10%） —— 三只基金平分
ATTACK_ALLOCATION = 0.10
SPLIT_FUNDS = 3

# 512810 单次买入 = 总资产 * 0.3%~0.4%（默认中点 0.35%）；可通过 ETFFRACTION_{LOW|HIGH} 覆盖
ETF_FRACTION_LOW  = float(os.getenv("ETFFRACTION_LOW",  "0.0030"))
ETF_FRACTION_HIGH = float(os.getenv("ETFFRACTION_HIGH", "0.0040"))
ETF_FRACTION_MID  = (ETF_FRACTION_LOW + ETF_FRACTION_HIGH) / 2.0

# 放量突破量能阈值倍数
VOL_MULTIPLIER = float(os.getenv("VOL_MULTIPLIER", "1.8"))

# 回撤买点阈值（区间）
DD_LOW  = float(os.getenv("DD_LOW",  "0.05"))  # 5%
DD_HIGH = float(os.getenv("DD_HIGH", "0.08"))  # 8%

# ATR 跟踪（用于将来卖点；此处仅计算并写入 params 供夜间用）
ATR_N = int(os.getenv("ATR_N", "10"))
ATR_COEFF = float(os.getenv("ATR_COEFF", "1.0"))  # 军工/半导体夜间可用到 1.2

# 执行时窗（上海）
WINDOW_START = (10, 0)
WINDOW_END   = (14, 45)

# 日志与状态
LOG_DIR  = "logs"
LOG_FILE = os.path.join(LOG_DIR, "signals.csv")

# 监控资产与代理
# quote_src: 'sina' 实时价量；kline_src: Eastmoney push2 历史K线（算20日高、均量、ATR）
# secid: Eastmoney push2 的证券ID，1=SH, 0=SZ；形如 "1.512810"
ASSETS = [
    {
        "asset_type": "FUND",
        "code": "022364",
        "name": "永赢科技智选A",
        # 用行业/主题ETF作代理：价格+量能+K线
        "proxies": [
            {"ticker": "0.159915", "sina": "sz159915", "weight": 0.6},  # 例：创业板ETF
            {"ticker": "1.588000", "sina": "sh588000", "weight": 0.4},  # 例：科创50ETF
        ],
        "sizing": {"mode": "fund_equal"}  # = TOTAL_ASSET * 10% / 3
    },
    {
        "asset_type": "FUND",
        "code": "006502",
        "name": "财通集成电路A",
        "proxies": [
            {"ticker": "1.512480", "sina": "sh512480", "weight": 0.6},  # 半导体ETF
            {"ticker": "0.159995", "sina": "sz159995", "weight": 0.4},  # 芯片ETF
        ],
        "sizing": {"mode": "fund_equal"}
    },
    {
        "asset_type": "FUND",
        "code": "018956",
        "name": "中航机遇领航A",
        "proxies": [
            {"ticker": "1.512810", "sina": "sh512810", "weight": 1.0},  # 军工ETF
        ],
        "sizing": {"mode": "fund_equal"}
    },
    {
        "asset_type": "ETF",
        "code": "512810",
        "name": "国防军工ETF",
        "self_etf": {"ticker": "1.512810", "sina": "sh512810"},
        "sizing": {"mode": "etf_fraction"}  # = TOTAL_ASSET * (0.3%~0.4%)（用中点）
    },
]

# ====== 工具函数 ===============================================================

def bj_now():
    # 上海时区 UTC+8
    return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=8)

def is_in_window(nowdt):
    h, m = nowdt.hour, nowdt.minute
    # 简化：只允许 10:00 ~ 14:45
    if (h, m) < WINDOW_START or (h, m) > WINDOW_END:
        return False
    # 周末不交易
    if nowdt.weekday() >= 5:
        return False
    return True

def ensure_dir(path): os.makedirs(path, exist_ok=True)

def read_today_existing_codes():
    if not os.path.exists(LOG_FILE):
        return set()
    today = bj_now().strftime("%Y-%m-%d")
    seen = set()
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date") == today and row.get("signal") in ("buy", "sell"):
                seen.add(row.get("code"))
    return seen

def log_signal(asset_type, code, name, signal, reason, price=None,
               day_pct=None, from_high_pct=None, size_amount=None, params=None):
    ensure_dir(LOG_DIR)
    header = [
        "time","date","asset_type","code","name","signal","reason",
        "price_or_nav","day_pct","from_high_pct","size_lots","size_amount","params"
    ]
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(header)
        now = bj_now()
        w.writerow([
            now.strftime("%H:%M:%S"),
            now.strftime("%Y-%m-%d"),
            asset_type, code, name, signal, reason,
            "" if price is None else price,
            "" if day_pct is None else day_pct,
            "" if from_high_pct is None else from_high_pct,
            "",  # size_lots 场内手数（场外为空）
            "" if size_amount is None else int(round(size_amount)),
            json.dumps(params or {}, ensure_ascii=False)
        ])

def server_chan_push(title, desp):
    key = os.getenv("SERVER_CHAN_KEY", "").strip()
    if not key:
        return
    try:
        requests.post(f"https://sctapi.ftqq.com/{key}.send",
                      data={"title": title, "desp": desp}, timeout=8)
    except Exception:
        pass

# ====== 行情获取：Sina 实时 & Eastmoney K线 ====================================

UA = {"User-Agent":"Mozilla/5.0", "Referer":"https://finance.sina.com.cn"}

def fetch_sina_quote(symbol):
    """
    symbol: 如 'sh512810' 'sz159915'
    返回：{price, pclose, volume, amount}
    """
    url = f"https://hq.sinajs.cn/list={symbol}"
    r = requests.get(url, headers=UA, timeout=8)
    r.encoding = "gbk"
    txt = r.text.split('="')[-1].strip('";\n')
    parts = txt.split(",")
    # ETF 字段：0 名称, 1 今天开盘, 2 昨收, 3 最新, 8 成交量(手), 9 成交额(元), 30 日期, 31 时间
    def fnum(s):
        try:
            return float(s)
        except Exception:
            return None
    price  = fnum(parts[3]) if len(parts) > 3 else None
    pclose = fnum(parts[2]) if len(parts) > 2 else None
    volume = fnum(parts[8]) if len(parts) > 8 else None  # 手
    amount = fnum(parts[9]) if len(parts) > 9 else None  # 元
    return {
        "price": price, "pclose": pclose,
        "volume": volume, "amount": amount,
        "date": parts[30] if len(parts)>30 else "",
        "time": parts[31] if len(parts)>31 else ""
    }

def fetch_em_kline(secid, lmt=60, klt=101):
    """
    Eastmoney push2 历史K线（日K=101）
    返回最近 lmt 条：list of dict {date, open, close, high, low, volume}
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&klt={klt}&fqt=1&lmt={lmt}&end=20500101&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58")
    r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=8)
    j = r.json()
    kl = (j.get("data") or {}).get("klines") or []
    out = []
    for row in kl:
        # "2025-08-15,0.722,0.727,0.729,0.712,1183980,85433.99"
        arr = row.split(",")
        if len(arr) < 7: 
            continue
        out.append({
            "date": arr[0],
            "open": float(arr[1]), "close": float(arr[2]),
            "high": float(arr[3]), "low": float(arr[4]),
            "volume": float(arr[5])  # 手
        })
    return out

def calc_ma(values, n):
    if len(values) < n or n <= 0: return None
    return sum(values[-n:]) / float(n)

def calc_atr(klines, n=10):
    if len(klines) < n+1: return None
    trs = []
    for i in range(1, len(klines)):
        h = klines[i]["high"]; l = klines[i]["low"]; pc = klines[i-1]["close"]
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
    if len(trs) < n: return None
    return sum(trs[-n:]) / float(n)

def session_elapsed_minutes(nowdt):
    """按 9:30-11:30, 13:00-15:00 的 240 分钟，返回当前已过分钟数（用于盘中量能线性折算）"""
    h, m = nowdt.hour, nowdt.minute
    total = 0
    # 上午
    if (h, m) <= (9, 30):
        return 0
    if (h, m) <= (11, 30):
        total += (h*60 + m) - (9*60 + 30)
        return total
    # 下午
    total += (11*60 + 30) - (9*60 + 30)  # 120
    if (h, m) < (13, 0):
        return total
    # 13:00 之后
    total += (h*60 + m) - (13*60 + 0)
    return min(total, 240)

# ====== 判定逻辑 ===============================================================

def weighted_proxy_quote(proxies):
    """返回加权的：price、pclose、volume、avg_vol20、high20、atr10（来自代理K线）"""
    total_w = sum(p["weight"] for p in proxies)
    if total_w <= 0: total_w = 1.0
    w_price = w_pclose = w_volume = 0.0
    # 体积线与锚点/ATR用第一个代理的K线为主；也可做加权K线，这里简化：
    primary = proxies[0]
    kls = fetch_em_kline(primary["ticker"], lmt=60, klt=101)
    high20 = max([k["close"] for k in kls[-20:]]) if len(kls) >= 20 else None
    avg_vol20 = calc_ma([k["volume"] for k in kls], 20)  # 手/日
    atr10 = calc_atr(kls, n=ATR_N)

    for p in proxies:
        q = fetch_sina_quote(p["sina"])
        w = p["weight"] / total_w
        if q["price"] is not None:
            w_price += w * q["price"]
        if q["pclose"] is not None:
            w_pclose += w * q["pclose"]
        if q["volume"] is not None:
            w_volume += w * q["volume"]
    return {
        "price": w_price or None,
        "pclose": w_pclose or None,
        "volume": w_volume or None,      # 手（盘中）
        "avg_vol20": avg_vol20 or None,  # 手/日
        "high20": high20 or None,
        "atr10": atr10 or None
    }

def self_etf_quote(self_etf):
    """512810 自身价量/锚点/均量/ATR"""
    kls = fetch_em_kline(self_etf["ticker"], lmt=60, klt=101)
    high20 = max([k["close"] for k in kls[-20:]]) if len(kls) >= 20 else None
    avg_vol20 = calc_ma([k["volume"] for k in kls], 20)
    atr10 = calc_atr(kls, n=ATR_N)
    q = fetch_sina_quote(self_etf["sina"])
    return {
        "price": q["price"], "pclose": q["pclose"],
        "volume": q["volume"],          # 手（盘中）
        "avg_vol20": avg_vol20,         # 手/日
        "high20": high20,
        "atr10": atr10
    }

def should_buy_by_drawdown(price, high20):
    if price is None or high20 is None or high20 <= 0:
        return (False, None)
    dd = 1.0 - (price / high20)
    return (DD_LOW <= dd <= DD_HIGH, dd)

def should_buy_by_breakout(price, high20, volume, avg_vol20, nowdt):
    if None in (price, high20, volume, avg_vol20):
        return (False, None)
    # 价格突破 20日高（留一点缓冲）
    if not (price > high20 * 1.001):
        return (False, None)
    # 盘中量能与日均量对齐（按时间进度线性折算）
    elapsed = session_elapsed_minutes(nowdt)
    if elapsed <= 0:
        return (False, None)
    scaled_need = VOL_MULTIPLIER * avg_vol20 * (elapsed / 240.0)
    return (volume >= scaled_need, None)

def fund_suggest_amount():
    # 主动基金：总资产 * 10% / 3
    return TOTAL_ASSET * ATTACK_ALLOCATION / float(SPLIT_FUNDS)

def etf_suggest_amount():
    # ETF 单次：总资产 * (0.3%~0.4%) 的中点
    return TOTAL_ASSET * ETF_FRACTION_MID

# ====== 主流程 =================================================================

def main():
    nowdt = bj_now()
    if not is_in_window(nowdt):
        print("out_of_window", nowdt)
        return

    seen_today = read_today_existing_codes()

    for a in ASSETS:
        code = a["code"]; name = a["name"]; a_type = a["asset_type"]
        if code in seen_today:
            # 当日同标的只提醒一次
            continue

        try:
            if a_type == "FUND":
                proxy = weighted_proxy_quote(a["proxies"])
                price = proxy["price"]; high20 = proxy["high20"]
                volume = proxy["volume"]; avg_vol20 = proxy["avg_vol20"]
                atr10 = proxy["atr10"]

                # A) 回撤
                by_dd, dd_val = should_buy_by_drawdown(price, high20)
                # B) 放量突破
                by_bo, _ = should_buy_by_breakout(price, high20, volume, avg_vol20, nowdt)

                if by_dd or by_bo:
                    size_amt = fund_suggest_amount()
                    reason = "drawdown_5_8" if by_dd else "vol_breakout_1p8"
                    params = {
                        "anchor_high20": high20,
                        "proxy_price": price,
                        "proxy_avg_vol20": avg_vol20,
                        "proxy_volume_intraday": volume,
                        "atr10": atr10,
                        "atr_coeff": ATR_COEFF
                    }
                    dd_pct = None if dd_val is None else round(dd_val*100, 2)
                    log_signal("FUND", code, name, "buy", reason,
                               price_or_nav=round(price, 4) if price else "",
                               day_pct=None, from_high_pct=dd_pct,
                               size_amount=size_amt, params=params)
                    title = f"BUY {code} {name}"
                    desp = f"{reason} | 参考价:{price:.4f} | 回撤:{dd_pct}% | 金额≈¥{int(round(size_amt))}"
                    server_chan_push(title, desp)

            elif a_type == "ETF":
                q = self_etf_quote(a["self_etf"])
                price = q["price"]; high20 = q["high20"]
                volume = q["volume"]; avg_vol20 = q["avg_vol20"]
                atr10 = q["atr10"]

                by_dd, dd_val = should_buy_by_drawdown(price, high20)
                by_bo, _ = should_buy_by_breakout(price, high20, volume, avg_vol20, nowdt)

                if by_dd or by_bo:
                    size_amt = etf_suggest_amount()
                    reason = "drawdown_5_8" if by_dd else "vol_breakout_1p8"
                    params = {
                        "anchor_high20": high20,
                        "price": price,
                        "avg_vol20": avg_vol20,
                        "volume_intraday": volume,
                        "atr10": atr10,
                        "atr_coeff": ATR_COEFF,
                        "event_exit": "2025-09-10"  # 阅兵后一周
                    }
                    dd_pct = None if dd_val is None else round(dd_val*100, 2)
                    log_signal("ETF", code, name, "buy", reason,
                               price_or_nav=round(price, 4) if price else "",
                               day_pct=None, from_high_pct=dd_pct,
                               size_amount=size_amt, params=params)
                    title = f"BUY {code} {name}"
                    desp = f"{reason} | 现价:{price:.4f} | 回撤:{dd_pct}% | 金额≈¥{int(round(size_amt))} | 阅兵后一周复核"
                    server_chan_push(title, desp)

            # 其它类型留空
        except Exception as e:
            print("error", code, type(e).__name__, str(e))
            continue

    print("done at", nowdt.strftime("%Y-%m-%d %H:%M:%S"))

# 兼容老版本 log_signal 的参数名
def log_signal(asset_type, code, name, signal, reason,
               price_or_nav=None, day_pct=None, from_high_pct=None, size_amount=None, params=None):
    ensure_dir(LOG_DIR)
    header = [
        "time","date","asset_type","code","name","signal","reason",
        "price_or_nav","day_pct","from_high_pct","size_lots","size_amount","params"
    ]
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(header)
        now = bj_now()
        w.writerow([
            now.strftime("%H:%M:%S"),
            now.strftime("%Y-%m-%d"),
            asset_type, code, name, signal, reason,
            "" if price_or_nav is None else price_or_nav,
            "" if day_pct is None else day_pct,
            "" if from_high_pct is None else from_high_pct,
            "",  # size_lots
            "" if size_amount is None else int(round(size_amount)),
            json.dumps(params or {}, ensure_ascii=False)
        ])

if __name__ == "__main__":
    main()
