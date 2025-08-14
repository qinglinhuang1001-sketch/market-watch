#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性监控（供 GitHub Actions 调用）
- 512810 ETF + 022364/006502/018956/018994 基金
- 买点/卖点触发 -> 微信推送 + 追加写入 logs/signals.csv
- 阅兵+7天清仓提醒
- 当日“只推一次/休眠”机制见 STOP_AFTER_FIRST_TRIGGER
"""

import os, time, json, datetime as dt, requests, warnings, csv
try:
    import urllib3
    warnings.filterwarnings("ignore", category=urllib3.exceptions.NotOpenSSLWarning)
except Exception:
    pass

# ===== 参数 =====
TOTAL_CAPITAL = 100000
STOP_AFTER_FIRST_TRIGGER = True
STRICT_FUND_WINDOW = True
FUND_DROP_CONFIRM = -1.0
TAKE_PROFIT_PCT   = 0.03
TRAIL_STOP_PCT    = 0.015
SELL_ONLY_AFTER_BUY = True

PARADE_DATE = "2025-09-03"
SELL_OFFSET_DAYS = 7
SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY","").strip()

ETF_CODE = "sh512810"
ETF_REF_HIGH = 0.727
ETF_MAIN_DROP_PCT = 5.0
ETF_SECOND_DROP = (-4.0, -2.0)
ETF_BID_ASK_TH = 0.90
ETF_MAIN_PCT = 0.035
ETF_SECOND_PCT = 0.018
ETF_BREAKOUT_PCT = 0.01

FUND_LIST = [
    ("022365","永赢科技智选C",       2.50, 5.0, 2.0, 4.0),
    ("006502","财通集成电路A",       2.00, 5.0, 2.0, 4.0),
    ("018956","中航机遇领航A",       2.30, 5.0, 2.0, 4.0),
    ("018994","中欧数字经济A(018994)",2.20, 5.0, 2.0, 4.0),
]
FUND_HARD_DROP = -5.0

POS_FILE   = "positions.json"
STATE_FILE = ".action_state.json"
LOG_DIR    = "logs"
LOG_FILE   = os.path.join(LOG_DIR, "signals.csv")

SINA_QUOTE = f"https://hq.sinajs.cn/list={ETF_CODE}"
SINA_HEADERS = {"Referer":"https://finance.sina.com.cn","User-Agent":"Mozilla/5.0"}

# ===== 公共函数 =====
def bj_now(): return dt.datetime.utcnow() + dt.timedelta(hours=8)
def ts(): return bj_now().strftime("%Y-%m-%d %H:%M:%S")
def today_str(): return bj_now().strftime("%Y-%m-%d")

def in_trading_time_beijing(now=None):
    now = now or bj_now()
    if now.weekday()>=5: return False
    t = now.time()
    return (dt.time(9,30)<=t<=dt.time(11,30)) or (dt.time(13,0)<=t<=dt.time(15,0))

def fund_time_ok():
    if not STRICT_FUND_WINDOW: return True
    t = bj_now().time()
    return dt.time(10,0) <= t <= dt.time(14,45)

def push_wechat(title, text):
    if not SERVER_CHAN_KEY: return
    try:
        requests.post(f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send",
                      data={"title":title,"desp":text},timeout=10)
    except Exception:
        pass

def ensure_log():
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE,"w",newline="",encoding="utf-8") as f:
            w=csv.writer(f)
            w.writerow(["time","date","asset_type","code","name","signal","reason",
                        "price_or_nav","day_pct","from_high_pct",
                        "size_lots","size_amount","params"])

def log_signal(asset_type, code, name, signal, reason,
               price_or_nav=None, day_pct=None, from_high_pct=None,
               size_lots=None, size_amount=None, params:dict=None):
    ensure_log()
    with open(LOG_FILE,"a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow([ts(), today_str(), asset_type, code, name, signal, reason,
                    None if price_or_nav is None else f"{price_or_nav:.6f}",
                    None if day_pct is None else f"{day_pct:.4f}",
                    None if from_high_pct is None else f"{from_high_pct:.4f}",
                    size_lots, size_amount,
                    json.dumps(params or {}, ensure_ascii=False)])

def load_state():
    base={"last_done":"","sell_notified_on":"","fund_state":{}}
    if os.path.exists(STATE_FILE):
        try: base.update(json.load(open(STATE_FILE,"r",encoding="utf-8")))
        except Exception: pass
    return base
def save_state(st): 
    try: json.dump(st, open(STATE_FILE,"w",encoding="utf-8"))
    except Exception: pass
def done_today(st): return st.get("last_done","")==today_str()
def mark_done_today(st): st["last_done"]=today_str(); save_state(st)

def load_positions():
    if os.path.exists(POS_FILE):
        try: return json.load(open(POS_FILE,"r",encoding="utf-8"))
        except Exception: pass
    return {}

def next_sell_date():
    base = dt.datetime.strptime(PARADE_DATE,"%Y-%m-%d").date()
    return base + dt.timedelta(days=SELL_OFFSET_DAYS)

def sell_reminder_if_needed(st):
    tgt = next_sell_date(); now=bj_now().date()
    if now>=tgt and st.get("sell_notified_on")!=today_str():
        msg=f"🛎 今日已到阅兵+{SELL_OFFSET_DAYS}天（目标 {tgt}）。按计划清仓 512810 波段持仓。"
        push_wechat("清仓提醒 | 阅兵+7", msg)
        log_signal("REMINDER","512810","阅兵清仓","reminder","parade+7")
        st["sell_notified_on"]=today_str(); save_state(st)

# ===== ETF =====
def fetch_etf_once():
    r=requests.get(SINA_QUOTE,headers=SINA_HEADERS,timeout=10); r.encoding="gbk"
    p=r.text.split('="')[-1].strip('";\n').split(',')
    name, now_p, prev = p[0], float(p[3] or 0), float(p[2] or 0)
    bids=[(float(p[i] or 0), int(p[i+1] or 0)) for i in range(10,20,2)]
    asks=[(float(p[i] or 0), int(p[i+1] or 0)) for i in range(20,30,2)]
    chg=(now_p-prev)/prev*100 if prev else 0.0
    return {"name":name,"now":now_p,"chg":chg,"bids":bids,"asks":asks}

def lots_by_pct(price,pct):
    LOT=100
    return max(int((TOTAL_CAPITAL*pct)//(price*LOT)),0)

def etf_judge_once(d):
    price, chg = d["now"], d["chg"]
    bid_sum=sum(q for _,q in d["bids"]); ask_sum=sum(q for _,q in d["asks"])
    bid_ok=(bid_sum>=ask_sum*ETF_BID_ASK_TH)
    cond_main   = (chg<=-ETF_MAIN_DROP_PCT) or (price<=ETF_REF_HIGH*(1-ETF_MAIN_DROP_PCT/100))
    cond_second = (ETF_SECOND_DROP[0]<=chg<=ETF_SECOND_DROP[1]) and bid_ok
    cond_break  = (price>ETF_REF_HIGH) and bid_ok
    triggered=False
    if cond_main:
        lots=lots_by_pct(price,ETF_MAIN_PCT)
        msg=f"现价{price:.3f}，日内{chg:.2f}%；建议买{lots}手。"
        push_wechat("512810 主要买点", msg)
        log_signal("ETF","512810","国防军工ETF","buy","main",price,chg,None,lots,None,
                   {"ref_high":ETF_REF_HIGH,"main_drop":ETF_MAIN_DROP_PCT})
        triggered=True
    elif cond_second:
        lots=lots_by_pct(price,ETF_SECOND_PCT)
        msg=f"现价{price:.3f}，日内{chg:.2f}%；买盘不弱；建议买{lots}手。"
        push_wechat("512810 次优买点", msg)
        log_signal("ETF","512810","国防军工ETF","buy","second",price,chg,None,lots,None,
                   {"band":ETF_SECOND_DROP,"bid_ask_th":ETF_BID_ASK_TH})
        triggered=True
    elif cond_break:
        lots=lots_by_pct(price,ETF_BREAKOUT_PCT)
        msg=f"现价{price:.3f} > 参考高点{ETF_REF_HIGH:.3f}；建议试探买{lots}手。"
        push_wechat("512810 突破试探", msg)
        log_signal("ETF","512810","国防军工ETF","buy","breakout",price,chg,None,lots,None,
                   {"ref_high":ETF_REF_HIGH})
        triggered=True
    return triggered

# ===== 基金 =====
def fetch_fund_gz(code):
    url=f"http://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    r=requests.get(url,timeout=10); t=r.text.strip()
    if not t.startswith("jsonpgz("): raise RuntimeError("fund gz bad resp")
    data=json.loads(t[len("jsonpgz("):-2])
    name=data.get("name",""); gsz=float(data.get("gsz","0") or 0); gszzl=float(data.get("gszzl","0") or 0)
    return name, gsz, gszzl

def fund_time_guard(): return (not STRICT_FUND_WINDOW) or fund_time_ok()

def fund_state_today(st, code):
    fs = st.setdefault("fund_state", {}).setdefault(code, {})
    if fs.get("date") != today_str():
        fs.update({"date":today_str(),"buy_pushed":False,"sell_pushed":False,
                   "entry":fs.get("entry"),"phase_high":fs.get("phase_high")})
    return fs

def can_push(fs, typ): return not fs.get(f"{typ}_pushed", False)
def set_entry(fs, price): fs["entry"]=price; fs["phase_high"]=price
def update_phase_high(fs, price):
    if "phase_high" in fs: fs["phase_high"]=max(fs["phase_high"],price)

def effective_entry(code, fs, pos_dict):
    if code in pos_dict and "cost" in pos_dict[code]: return float(pos_dict[code]["cost"])
    return fs.get("entry")

def fund_buy_logic(code,name,gsz,gszzl,ref_high,main_drop,sec_low,sec_high,fs,pos_dict):
    if not fund_time_guard(): return False
    if (FUND_DROP_CONFIRM is not None) and (gszzl>FUND_DROP_CONFIRM): return False
    from_high=(1-gsz/ref_high)*100 if ref_high else 0
    cond_hard=(gszzl<=FUND_HARD_DROP)
    cond_main=(from_high>=main_drop)
    cond_second=(sec_low<=from_high<=sec_high)
    if not can_push(fs,"buy"): return False
    if cond_hard or cond_main or cond_second:
        amt=TOTAL_CAPITAL*(0.035 if (cond_hard or cond_main) else 0.018)
        label="主要买点" if (cond_hard or cond_main) else "次优买点"
        push_wechat(f"{code} {label}", f"{name} 回撤 {from_high:.2f}%；当日估值 {gszzl:.2f}%；建议买≈{amt:.0f}元。")
        log_signal("FUND",code,name,"buy","main" if (cond_hard or cond_main) else "second",
                   gsz,gszzl,from_high,None,int(amt),
                   {"ref_high":ref_high,"confirm":FUND_DROP_CONFIRM})
        fs["buy_pushed"]=True
        if effective_entry(code,fs,pos_dict) is None: set_entry(fs, gsz)
        return True
    return False

def fund_sell_logic(code,name,gsz,gszzl,fs,pos_dict):
    entry=effective_entry(code,fs,pos_dict)
    if SELL_ONLY_AFTER_BUY and (entry is None): return False
    if entry is None: return False
    update_phase_high(fs, gsz)
    phase_high=fs.get("phase_high", gsz)
    profit=(gsz/entry)-1.0
    drawdown=1.0-(gsz/phase_high if phase_high else 1.0)
    cond_tp   = (profit>=TAKE_PROFIT_PCT)
    cond_trail= (drawdown>=TRAIL_STOP_PCT)
    if can_push(fs,"sell") and (cond_tp or cond_trail):
        reason=f"止盈 {profit*100:.2f}%" if cond_tp else f"回撤 {drawdown*100:.2f}%"
        push_wechat(f"{code} 卖出信号", f"{name} {reason} 触发；entry≈{entry:.4f}，现估{gsz:.4f}。")
        log_signal("FUND",code,name,"sell","take_profit" if cond_tp else "trail_stop",
                   gsz,gszzl,None,None,None,
                   {"entry":entry,"phase_high":phase_high,
                    "tp":TAKE_PROFIT_PCT,"ts":TRAIL_STOP_PCT})
        fs["sell_pushed"]=True
        return True
    return False

# ===== 主流程 =====
def main():
    st=load_state()
    sell_reminder_if_needed(st)
    if STOP_AFTER_FIRST_TRIGGER and done_today(st): return
    if not in_trading_time_beijing(): return

    pos_dict=load_positions()
    any_triggered=False

    # ETF
    try:
        d=fetch_etf_once()
        if etf_judge_once(d): any_triggered=True
    except Exception: pass

    # FUNDS
    for code,name,ref_high,mdrop,slow,shigh in FUND_LIST:
        try:
            fs=fund_state_today(st, code)
            nm, gsz, gszzl = fetch_fund_gz(code); nm = nm or name
            if fund_buy_logic(code,nm,gsz,gszzl,ref_high,mdrop,slow,shigh,fs,pos_dict):
                any_triggered=True
            if fund_sell_logic(code,nm,gsz,gszzl,fs,pos_dict):
                any_triggered=True
        except Exception: pass

    if any_triggered and STOP_AFTER_FIRST_TRIGGER: mark_done_today(st)
    else: save_state(st)

if __name__=="__main__":
    main()
