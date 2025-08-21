# check_once.py
# 盘中监控：512810(国防军工ETF)波段买点 + 三只场外基金估值信号；Server酱推送

import os, json, time, math, datetime
from pathlib import Path
import requests
import pytz

# ---------- 环境&目录 ----------
TZ = pytz.timezone("Asia/Shanghai")
TODAY = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
ROOT = Path(".")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = ROOT / ".action_state.json"    # 去重状态

# ---------- 监控清单 ----------
FUND_CODES = os.getenv("FUND_CODES", "022364,006502,018956").split(",")
ETF_CODES  = os.getenv("ETF_CODES",  "512810,513530,159399").split(",")

# 默认只对 512810 做波段逻辑（其他 ETF 可按需复用）
FOCUS_ETF = "512810"

# ---------- 资金管理 ----------
def _safe_float(x, default):
    try:
        return float(x)
    except:
        return default

TOTAL_ASSETS = _safe_float(os.getenv("TOTAL_ASSETS", ""), 100000.0)
ATTACK_PCT_FUNDS = _safe_float(os.getenv("ATTACK_PCT_FUNDS", "0.10"), 0.10)
ATTACK_PCT_ETF_MIN = _safe_float(os.getenv("ATTACK_PCT_ETF_MIN", "0.03"), 0.03)
ATTACK_PCT_ETF_MAX = _safe_float(os.getenv("ATTACK_PCT_ETF_MAX", "0.04"), 0.04)
ETF_ATTACK = (ATTACK_PCT_ETF_MIN + ATTACK_PCT_ETF_MAX) / 2.0

# ---------- 参数 ----------
PULLBACK_MIN = _safe_float(os.getenv("PULLBACK_MIN", "0.05"), 0.05)  # 5%
PULLBACK_MAX = _safe_float(os.getenv("PULLBACK_MAX", "0.08"), 0.08)  # 8%
VOL_BREAKOUT = _safe_float(os.getenv("VOL_BREAKOUT", "1.8"), 1.8)

# 军事阅兵日（提醒一周后清仓）
PARADE_DATE = os.getenv("PARADE_DATE", "2025-09-03")
sell_remind_day = (datetime.datetime.strptime(PARADE_DATE, "%Y-%m-%d") + datetime.timedelta(days=7)).strftime("%Y-%m-%d")

# ---------- 通知 ----------
SCT_KEY = os.getenv("SCT_KEY", "").strip()

def notify(title: str, text: str):
    print(f"[notify]\n{title}\n{text}\n")
    if not SCT_KEY:
        return
    try:
        url = f"https://sctapi.ftqq.com/{SCT_KEY}.send"
        data = {"title": title, "desp": text}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Server酱推送失败：", e)

# ---------- 工具 ----------
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}

def sina_symbol(code: str) -> str:
    if code.startswith(("5","6")):
        return f"sh{code}"
    return f"sz{code}"

def fetch_etf_quote(code: str):
    """新浪 ETF 行情：价格/昨收/总量/时间"""
    sym = sina_symbol(code)
    url = f"https://hq.sinajs.cn/list={sym}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    txt = r.text
    parts = txt.split("=")[-1].strip('";\n').split(",")
    if len(parts) < 10 or parts[0] == "":
        return None
    name = parts[0]
    pre_close = float(parts[2] or 0.0)
    price = float(parts[3] or 0.0)
    high = float(parts[4] or 0.0)
    low = float(parts[5] or 0.0)
    volume = float(parts[8] or 0.0)  # 手
    date = parts[-3] if len(parts) >= 3 else TODAY
    tm   = parts[-2] if len(parts) >= 2 else ""
    pct = 0.0 if pre_close == 0 else (price - pre_close) / pre_close
    return {
        "name": name, "price": price, "pre_close": pre_close, "pct": pct,
        "high": high, "low": low, "volume": volume, "date": date, "time": tm
    }

def eastmoney_fund_est(code: str):
    """东财基金估值：返回估值价&估值涨跌幅"""
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    r = requests.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
    if r.status_code != 200 or "jsonpgz" not in r.text:
        return None
    import re, json
    m = re.search(r"jsonpgz\((\{.*\})\)", r.text)
    if not m:
        return None
    data = json.loads(m.group(1))
    gsz = float(data.get("gsz") or 0.0)
    gz  = data.get("gztime","")
    gz_pct = float(data.get("gszzl") or 0.0)/100.0
    return {"value": gsz, "pct": gz_pct, "time": gz}

def minutes_since_open(now: datetime.datetime):
    # 交易日假定：09:30-11:30, 13:00-15:00
    h, m = now.hour, now.minute
    minutes = 0
    # 上午
    if h < 9 or (h == 9 and m < 30):
        return 0
    if h < 11 or (h == 11 and m <= 30):
        minutes += (h - 9) * 60 + (m - 30)
        return max(0, minutes)
    # 下午
    minutes += 120  # 上午 2 小时
    if h < 13:
        return minutes
    if h >= 15:
        return 240
    minutes += (h - 13) * 60 + m
    return max(0, minutes)

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- 规则 ----------
def etf_wave_buy(code: str, q: dict, state: dict):
    """512810 波段买点：回撤 5%~8% 或 放量突破"""
    # 1) 参考高点：默认取当日 high（可替换为近 N 日高点，或从 state['ref_high'] 持久化）
    ref_high = max(q["high"], state.get(f"refhigh_{code}", 0))
    if ref_high == 0:
        ref_high = q["price"]
    state[f"refhigh_{code}"] = ref_high

    pullback = 0.0 if ref_high == 0 else (ref_high - q["price"]) / ref_high
    pb_ok = (pullback >= PULLBACK_MIN) and (pullback <= PULLBACK_MAX)

    # 2) 放量突破（估算分钟均量）
    now = datetime.datetime.now(TZ)
    mins = max(1, minutes_since_open(now))
    avg_per_min = q["volume"] / mins if mins > 0 else 0
    # 当前近似“此刻分钟量”：用总量增长速度近似（无逐笔数据只能近似推断）
    # 这里采用“总量/分钟均量”的倍数判断
    vol_ok = avg_per_min > 0 and (q["volume"] / (avg_per_min * mins)) > VOL_BREAKOUT

    # 触发一次即去重
    fired_key = f"etf_fired_{code}_{TODAY}"
    if pb_ok or vol_ok:
        if not state.get(fired_key):
            amt = TOTAL_ASSETS * ETF_ATTACK
            title = f"BUY {code} {q['name']}"
            reason = []
            if pb_ok:
                reason.append(f"回撤区间 {PULLBACK_MIN:.0%}~{PULLBACK_MAX:.0%}，当前回撤 {pullback:.2%}")
            if vol_ok:
                reason.append(f"放量突破（> {VOL_BREAKOUT:.1f}×均量）")
            text = (
f"""价格: {q['price']:.3f}（涨跌: {q['pct']:.2%}）
参考高点: {ref_high:.3f}
建议买入约: ￥{amt:,.0f}（占总资产 {ETF_ATTACK:.0%}）
阅兵后一周({sell_remind_day}) 将自动推送清仓提醒。"""
            )
            notify(title, "；".join(reason) + "\n" + text)
            state[fired_key] = True

def fund_signal(code: str, state: dict):
    """场外基金估值回撤到区间"""
    est = eastmoney_fund_est(code)
    if not est:
        return
    # 简化规则：估值跌幅 >= 2%（回撤阈值你可以自己调），示例为 -2% 触发
    pb_ok = est["pct"] <= -0.02

    fired_key = f"fund_fired_{code}_{TODAY}"
    if pb_ok and not state.get(fired_key):
        amt = TOTAL_ASSETS * ATTACK_PCT_FUNDS
        title = f"BUY {code} 场外基金"
        text = f"估值: {est['value']:.4f}，估值涨跌: {est['pct']:.2%}\n建议买入约: ￥{amt:,.0f}（占总资产 {ATTACK_PCT_FUNDS:.0%}）"
        notify(title, text)
        state[fired_key] = True

def sell_reminder(state: dict):
    """阅兵后一周清仓提醒（仅提醒一次）"""
    key = f"parade_sold_{sell_remind_day}"
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    if today == sell_remind_day and not state.get(key):
        notify("清仓提醒", f"已到 {sell_remind_day}（阅兵后一周），请评估激进仓位的减仓/清仓。")
        state[key] = True

def main():
    st = load_state()

    # ETF：重点监控 512810 波段
    if FOCUS_ETF in ETF_CODES:
        q = fetch_etf_quote(FOCUS_ETF)
        if q:
            etf_wave_buy(FOCUS_ETF, q, st)

    # 场外基金三只
    for c in FUND_CODES:
        c = c.strip()
        if c:
            fund_signal(c, st)

    sell_reminder(st)
    save_state(st)

if __name__ == "__main__":
    main()
