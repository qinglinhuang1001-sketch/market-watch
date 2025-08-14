#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端一次性脚本（给 GitHub Actions 调用）：
- 512810 场内ETF买点（主要/次优/突破）
- 022364 / 006502 / 018956 / 018994 四只基金估值型买点
- 阅兵+7天清仓提醒
- ★ 当日首次触发后，记录到 .action_state.json；当天后续运行直接退出
"""

import os, time, json, datetime as dt, requests, warnings
# 静默 urllib3 在 macOS 上的 LibreSSL 警告（GitHub 也没关系，安全）
try:
    import urllib3
    warnings.filterwarnings("ignore", category=urllib3.exceptions.NotOpenSSLWarning)
except Exception:
    pass

# ========= 你的参数（可按需调整） =========
TOTAL_CAPITAL = 100000         # 进攻仓总资金（元）

PARADE_DATE = "2025-09-03"     # 阅兵日
SELL_OFFSET_DAYS = 7           # +7天推送清仓提醒

# Server酱 SendKey（从 GitHub Actions Secret 传入）
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "").strip()

# —— ETF（512810）参数 ——（一次性检查）
ETF_CODE = "sh512810"
ETF_REF_HIGH = 0.727           # 最近参考高点（按需改）
ETF_MAIN_DROP_PCT = 5.0        # 主要买点：跌幅<=-5% 或 价<=REF_HIGH*(1-5%)
ETF_SECOND_DROP = (-4.0, -2.0) # 次优买点：当日跌幅在[-4%,-2%] 且买盘不弱
ETF_BID_ASK_TH = 0.90          # 买盘Σ >= 卖盘Σ*90% 视为“买盘不弱”
ETF_MAIN_PCT = 0.035           # 主要买点仓位
ETF_SECOND_PCT = 0.018         # 次优买点仓位
ETF_BREAKOUT_PCT = 0.01        # 突破试探仓位

# —— 场外基金（东财实时估值）——
# (code, name, ref_high, main_drop%=5, second_low%=2, second_high%=4)
FUND_LIST = [
    ("022365", "永赢科技智选C", 2.50, 5.0, 2.0, 4.0),
]
FUND_HARD_DROP = -5.0          # 当日估值跌幅<=-5% 硬触发提醒

# ========= 常量 =========
STATE_FILE = ".action_state.json"   # 持久状态：当日是否已触发
SINA_QUOTE = f"https://hq.sinajs.cn/list={ETF_CODE}"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

# ========= 工具函数 =========
def bj_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)

def today_str():
    return bj_now().strftime("%Y-%m-%d")

def in_trading_time_beijing(now=None):
    """工作日 09:30-11:30 / 13:00-15:00（北京时区）"""
    now = now or bj_now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dt.time(9,30) <= t <= dt.time(11,30)) or (dt.time(13,0) <= t <= dt.time(15,0))

def push_wechat(title, text):
    if not SERVER_CHAN_KEY:
        return
    try:
        url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
        requests.post(url, data={"title": title, "desp": text}, timeout=10)
    except Exception:
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE,"r",encoding="utf-8"))
        except Exception:
            pass
    return {"last_done": "", "sell_notified_on": ""}

def save_state(st: dict):
    try:
        json.dump(st, open(STATE_FILE,"w",encoding="utf-8"))
    except Exception:
        pass

def done_today() -> bool:
    st = load_state()
    return st.get("last_done","") == today_str()

def mark_done_today():
    st = load_state()
    st["last_done"] = today_str()
    save_state(st)

# ========= 阅兵+7天 提醒 =========
def next_sell_date():
    base = dt.datetime.strptime(PARADE_DATE, "%Y-%m-%d").date()
    return base + dt.timedelta(days=SELL_OFFSET_DAYS)

def sell_reminder_if_needed():
    st = load_state()
    tgt = next_sell_date()
    now = bj_now().date()
    # 每天最多提醒一次
    if now >= tgt and st.get("sell_notified_on") != today_str():
        push_wechat("清仓提醒 | 阅兵+7",
                    f"🛎 今日已到阅兵+{SELL_OFFSET_DAYS}天（目标 {tgt}）。按计划清仓 512810 波段持仓。")
        st["sell_notified_on"] = today_str()
        save_state(st)

# ========= 512810（一次性检查）=========
def fetch_etf_once():
    r = requests.get(SINA_QUOTE, headers=SINA_HEADERS, timeout=10)
    r.encoding = "gbk"
    p = r.text.split('="')[-1].strip('";\n').split(',')
    name, now_p, prev = p[0], float(p[3] or 0), float(p[2] or 0)
    # 买卖一到五
    bids = [(float(p[i] or 0), int(p[i+1] or 0)) for i in range(10,20,2)]
    asks = [(float(p[i] or 0), int(p[i+1] or 0)) for i in range(20,30,2)]
    chg = (now_p - prev) / prev * 100 if prev else 0.0
    return {"name":name,"now":now_p,"chg":chg,"bids":bids,"asks":asks}

def lots_by_pct(price: float, pct: float) -> int:
    LOT = 100
    lots = int((TOTAL_CAPITAL * pct) // (price * LOT))
    return max(lots, 0)

def etf_judge_once(d) -> bool:
    """返回是否触发过任何提醒"""
    price, chg = d["now"], d["chg"]
    bid_sum = sum(q for _, q in d["bids"]); ask_sum = sum(q for _, q in d["asks"])
    bid_ok  = (bid_sum >= ask_sum * ETF_BID_ASK_TH)
    triggered = False

    cond_main   = (chg <= -ETF_MAIN_DROP_PCT) or (price <= ETF_REF_HIGH*(1-ETF_MAIN_DROP_PCT/100))
    cond_second = (ETF_SECOND_DROP[0] <= chg <= ETF_SECOND_DROP[1]) and bid_ok
    cond_break  = (price > ETF_REF_HIGH) and bid_ok

    if cond_main:
        lots = lots_by_pct(price, ETF_MAIN_PCT)
        push_wechat("512810 主要买点", f"现价{price:.3f}，日内{chg:.2f}%；建议买{lots}手。")
        triggered = True
    elif cond_second:
        lots = lots_by_pct(price, ETF_SECOND_PCT)
        push_wechat("512810 次优买点", f"现价{price:.3f}，日内{chg:.2f}%；买盘不弱；建议买{lots}手。")
        triggered = True
    elif cond_break:
        lots = lots_by_pct(price, ETF_BREAKOUT_PCT)
        push_wechat("512810 突破试探", f"现价{price:.3f} > 参考高点{ETF_REF_HIGH:.3f}；建议试探买{lots}手。")
        triggered = True

    return triggered

# ========= 基金（一次性检查）=========
def fetch_fund_gz(code: str):
    # 东财实时估值 JSONP
    url = f"http://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    r = requests.get(url, timeout=10)
    t = r.text.strip()
    if not t.startswith("jsonpgz("):
        raise RuntimeError("fund gz bad resp")
    data = json.loads(t[len("jsonpgz("):-2])
    name = data.get("name","")
    gsz  = float(data.get("gsz","0") or 0)        # 估算净值
    gszzl= float(data.get("gszzl","0") or 0)      # 当日估算涨跌幅 %
    return name, gsz, gszzl

def fund_check_once(code, name, ref_high, main_drop, sec_low, sec_high) -> bool:
    nm, gsz, gszzl = fetch_fund_gz(code)
    nm = nm or name
    from_high = (1 - gsz/ref_high)*100 if ref_high else 0
    triggered = False
    cond_main   = (from_high >= main_drop)
    cond_second = (sec_low <= from_high <= sec_high)
    cond_hard   = (gszzl <= FUND_HARD_DROP)

    if cond_hard:
        push_wechat(f"{code} 日内估值大跌", f"{nm} 估值当日 {gszzl:.2f}%")
        triggered = True
    if cond_main:
        amt = TOTAL_CAPITAL * 0.035
        push_wechat(f"{code} 主要买点", f"{nm} 距参高回撤 {from_high:.2f}%（≥{main_drop}%），建议买≈{amt:.0f}元。")
        triggered = True
    elif cond_second:
        amt = TOTAL_CAPITAL * 0.018
        push_wechat(f"{code} 次优买点", f"{nm} 回撤 {from_high:.2f}%（{sec_low}%~{sec_high}%），建议买≈{amt:.0f}元。")
        triggered = True
    return triggered

# ========= 主流程（一次性）=========
def main():
    # 清仓提醒（每天最多一次）
    sell_reminder_if_needed()

    # 当天已经触发过 → 直接退出
    if done_today():
        return

    # 只在交易时段内检查
    if not in_trading_time_beijing():
        return

    any_triggered = False

    # ETF
    try:
        d = fetch_etf_once()
        if etf_judge_once(d):
            any_triggered = True
    except Exception:
        pass

    # 基金
    for code, name, ref_high, mdrop, slow, shigh in FUND_LIST:
        try:
            if fund_check_once(code, name, ref_high, mdrop, slow, shigh):
                any_triggered = True
        except Exception:
            pass

    # 标记当日已触发
    if any_triggered:
        mark_done_today()

if __name__ == "__main__":
    main()
