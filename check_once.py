#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端一次性脚本（2分钟触发一次）：
- 512810 场内ETF买点（主要/次优/突破）
- 022364 / 006502 / 018956 三只基金估值型买点
- 阅兵+7天清仓提醒
- ★新增：若当日已触发过提醒，则本次直接退出（状态保存在仓库 .action_state.json）
"""

import os, time, json, datetime as dt, requests, warnings
try:
    import urllib3
    warnings.filterwarnings("ignore", category=urllib3.exceptions.NotOpenSSLWarning)
except Exception:
    pass

# ===== 你的参数 =====
TOTAL_CAPITAL = 100000

PARADE_DATE = "2025-09-03"
SELL_OFFSET_DAYS = 7

SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "").strip()

ETF_CODE = "sh512810"
ETF_REF_HIGH = 0.727
ETF_MAIN_DROP_PCT = 5.0
ETF_SECOND_DROP = (-4.0, -2.0)
ETF_BID_ASK_TH = 0.90
ETF_MAIN_PCT = 0.035
ETF_SECOND_PCT = 0.018
ETF_BREAKOUT_PCT = 0.01

FUND_LIST = [
    ("022364", "永赢科技智选A", 2.50, 5.0, 2.0, 4.0),
    ("006502", "财通集成电路A", 2.00, 5.0, 2.0, 4.0),
    ("018956", "中航机遇领航A", 2.30, 5.0, 2.0, 4.0),
    ("018994", "新基金018994", 2.20, 5.0, 2.0, 4.0),
]

FUND_HARD_DROP = -5.0

STATE_FILE = ".action_state.json"   # 持久状态：当日是否已触发
SINA_QUOTE = f"https://hq.sinajs.cn/list={ETF_CODE}"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

# ===== 共用工具 =====
def bj_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)

def today_str():
    return bj_now().strftime("%Y-%m-%d")

def in_trading_time_beijing(now=None):
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
        requests.post(url, data={"title": title, "desp": text}, timeout=8)
    except Exception:
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE,"r",encoding="utf-8"))
        except Exception:
            pass
    return {"last_done": ""}

def save_state(st: dict):
    try:
        json.dump(st, open(STATE_FILE,"w",encoding="utf-8"))
    except Exception:
        pass

def mark_done_today():
    st = load_state()
    st["last_done"] = today_str()
    save_state(st)

def done_today() -> bool:
    st = load_state()
    return st.get("last_done","") == today_str()

# ===== 阅兵+7天 提醒 =====
def next_sell_datetime():
    base = dt.datetime.strptime(PARADE_DATE, "%Y-%m-%d")
    return base + dt.timedelta(days=SELL_OFFSET_DAYS)

def sell_reminder_if_needed():
    st = load_state()
    tgt = next_sell_datetime().date()
    now = bj_now().date()
    # 同一天只发一次
    key = "sell_notified_on"
    if now >= tgt and st.get(key) != today_str():
        push_wechat("清仓提醒 | 阅兵+7", f"🛎 今日已到阅兵+{SELL_OFFSET_DAYS}天（目标 {tgt}）。请按计划清仓 512810 波段持仓。")
        st[key] = today_str()
        save_state(st)

# ===== 512810（一次性检查）=====
def fetch_etf_once():
    r = requests.get(SINA_QUOTE, headers=SINA_HEADERS, timeout=8)
    r.encoding = "gbk"
    p = r.text.split('="')[-1].strip('";\n').split(',')
    name, now_p, prev = p[0], float(p[3] or 0), float(p[2] or 0)
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

# ===== 基金（一次性检查）=====
def fetch_fund_gz(code: str):
    url = f"http://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    r = requests.get(url, timeout=8)
    t = r.text.strip()
    if not t.startswith("jsonpgz("):
        raise RuntimeError("fund gz bad resp")
    data = json.loads(t[len("jsonpgz("):-2])
    name = data.get("name","")
    gsz  = float(data.get("gsz","0") or 0)
    gszzl= float(data.get("gszzl","0") or 0)
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

# ===== 主流程（一次性）=====
def main():
    # 1) 阅兵+7天清仓提醒（每天最多一次）
    sell_reminder_if_needed()

    # 2) 如果今天已经触发过任何提醒 → 直接退出
    if done_today():
        return

    # 3) 只在交易时段内检查
    if not in_trading_time_beijing():
        return

    any_triggered = False

    # 3.1 ETF
    try:
        d = fetch_etf_once()
        if etf_judge_once(d):
            any_triggered = True
    except Exception:
        pass

    # 3.2 基金
    for code, name, ref_high, mdrop, slow, shigh in FUND_LIST:
        try:
            if fund_check_once(code, name, ref_high, mdrop, slow, shigh):
                any_triggered = True
        except Exception:
            pass

    # 4) 若命中 → 标记当日已完成
    if any_triggered:
        mark_done_today()

if __name__ == "__main__":
    main()
