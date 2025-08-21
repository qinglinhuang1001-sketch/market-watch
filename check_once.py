# -*- coding: utf-8 -*-
"""
check_once.py
盘中一次检测：
- 场外基金（022364/006502/018956）盘中预警：名称校验 + 新鲜度 + 可选ETF方向交验 + 连续确认 + 冷却 + 等权资金测算
- 场内ETF（默认 512810）波段：回撤区间（相对参考高点）+ 可选放量近似过滤 + 冷却
- Server酱 Turbo 推送（可选）
- 日内去重：.action_state.json

环境变量（建议在 GitHub Actions Variables/Secrets 配置）：
# 通用
TOTAL_ASSETS=100000          # 总资产（元）
SCTKEY=                      # Server酱 Turbo Key（可选，不填则仅打印日志）
TZ=Asia/Shanghai             # 时区，用于“新鲜度/交易时段”判断；默认沪深

# 交易时段控制
INTRADAY_ONLY=1              # 1=仅交易时段内才触发；0=任意时间

# ====== 场外基金盘中预警开关与参数 ======
FUND_INTRADAY=true           # 开启基金盘中预警；false 关闭
FUND_EST_FRESH_SEC=1500      # 估值新鲜度阈值（秒），默认 25 分钟
FUND_CONFIRM_ROUNDS=2        # 连续确认轮数（避免单点尖峰）
FUND_COOLDOWN_MIN=60         # 单只冷却时间（分钟）
FUND_BUY_RATIO=0.10          # 进攻仓按总资产的 10%（单只命中时的买入比例）
# （可选）用行业/主题ETF做方向一致性交验（只看方向，不卡幅度差）：
# JSON，例： {"006502":["sz159995"],"022364":["sh515000","sz159915"],"018956":["sh512810"]}
FUND_PROXY_MAP={"006502":["sz159995"],"022364":[], "018956":["sh512810"]}

# ====== ETF 波段（默认 512810 国防军工） ======
ETF_INTRADAY=true
WATCH_ETF=512810             # 逗号分隔
REF_HIGH_512810=0.727        # 参考高点（必配，或运行中会自动用当日high兜底但精度较差）
DRAW_MIN=-0.08               # 回撤区间下限（-8%）
DRAW_MAX=-0.05               # 回撤区间上限（-5%）
VOL_MULT=1.8                 # 放量突破近似阈值（>1.8x）
ETF_COOLDOWN_MIN=30          # ETF 冷却（分钟）
ETF_SLICE=0.035              # ETF 建议买入金额 = TOTAL_ASSETS * 0.10 * ETF_SLICE

注意：你可以按需把 FUND_WATCH 里的配置改成你自己的默认口径（在代码里）。
"""

import os
import re
import json
import time
import math
import datetime as dt
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

import requests

# =========================
# 基础 & 环境
# =========================
def _to_float(v, default=0.0) -> float:
    try:
        s = str(v).strip().replace(",", "")
        return float(s)
    except Exception:
        return default

TZ_NAME = os.getenv("TZ", "Asia/Shanghai")
try:
    import zoneinfo  # py3.9+
    TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception:
    TZ = None  # 没安装也不影响

def now_cn() -> dt.datetime:
    n = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    if TZ:
        return n.astimezone(TZ)
    return n + dt.timedelta(hours=8)

INTRADAY_ONLY = os.getenv("INTRADAY_ONLY", "1") == "1"
TOTAL_ASSETS = _to_float(os.getenv("TOTAL_ASSETS", "100000"), 100000.0)
SCTKEY = os.getenv("SCTKEY", "").strip()

ROOT = Path(".")
STATE_FILE = ROOT / ".action_state.json"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept": "*/*",
    "Connection": "keep-alive"
})

def within_trading_time(ts: dt.datetime) -> bool:
    """沪深交易时段：周一~周五 09:20-11:35，12:55-15:10"""
    if not INTRADAY_ONLY:
        return True
    t = ts if TZ else ts + dt.timedelta(hours=8)
    if t.weekday() >= 5:
        return False
    hm = t.strftime("%H:%M")
    return ("09:20" <= hm <= "11:35") or ("12:55" <= hm <= "15:10")

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

# =========================
# 推送（Server酱 Turbo）
# =========================
def serverchan_push(title: str, desp: str) -> None:
    print(f"\n[ALERT] {title}\n{desp}\n")
    if not SCTKEY:
        return
    url = f"https://sctapi.ftqq.com/{SCTKEY}.send"
    data = {"title": title, "desp": desp}
    try:
        requests.post(url, data=data, timeout=8)
    except Exception:
        pass

# =========================
# 新浪 ETF 行情
# =========================
def _sina_code(code: str) -> str:
    c = code.strip()
    if c.startswith(("sh", "sz")):
        return c
    if len(c) == 6:
        return ("sz" + c) if c[0] in ("0", "1", "2", "3") else ("sh" + c)
    return c

def fetch_sina_quote(sinacode: str) -> Optional[Dict[str, Any]]:
    if not sinacode:
        return None
    url = f"http://hq.sinajs.cn/list={_sina_code(sinacode)}"
    try:
        r = SESSION.get(url, timeout=6, headers={"Referer": "https://finance.sina.com.cn"})
        r.raise_for_status()
        m = re.search(r'="([^"]*)";', r.text)
        if not m:
            return None
        parts = m.group(1).split(",")
        if len(parts) < 10:
            return None
        name = parts[0]
        open_ = _to_float(parts[1], 0)
        preclose = _to_float(parts[2], 0)
        price = _to_float(parts[3], 0)
        high = _to_float(parts[4], 0)
        low = _to_float(parts[5], 0)
        volume_hand = _to_float(parts[8], 0)  # 手
        amount = _to_float(parts[9], 0)       # 元
        pct = 0.0 if preclose <= 0 else (price - preclose) / preclose * 100.0
        return {
            "name": name, "open": open_, "preclose": preclose, "price": price,
            "high": high, "low": low, "volume_hand": volume_hand, "amount": amount,
            "pct": pct
        }
    except Exception:
        return None

def minutes_since_open(ts: dt.datetime) -> int:
    """粗略推交易已过分钟（沪深）"""
    t = ts if TZ else ts + dt.timedelta(hours=8)
    h, m = t.hour, t.minute
    # 上午 09:30-11:30
    if h < 9 or (h == 9 and m < 30):
        return 0
    total = 0
    if h < 11 or (h == 11 and m <= 30):
        total += (h - 9) * 60 + (m - 30)
        return max(0, total)
    total += 120
    # 下午 13:00-15:00
    if h < 13:
        return total
    if h >= 15:
        return 240
    total += (h - 13) * 60 + m
    return max(0, total)

# =========================
# 天天基金估值（带名称校验+新鲜度）
# =========================
def fetch_fund_estimate(code: str, expected_name_substr: Optional[str] = None) -> Optional[Dict[str, Any]]:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    try:
        r = SESSION.get(url, timeout=6, headers={"Referer": "https://fund.eastmoney.com"})
        r.raise_for_status()
        m = re.search(r"jsonpgz\((\{.*?\})\);", r.text)
        if not m:
            return None
        data = json.loads(m.group(1))
        name = data.get("name", "") or ""
        if expected_name_substr and (expected_name_substr not in name):
            return None  # 名称不匹配，丢弃，防 code 误映射
        gsz = _to_float(data.get("gsz"), None)
        pct = _to_float(data.get("gszzl"), None)  # %
        gztime = data.get("gztime", "")  # "YYYY-MM-DD HH:MM"
        if gsz is None or pct is None:
            return None
        # 新鲜度（分钟）
        fresh_min = 1e9
        try:
            t = dt.datetime.strptime(gztime, "%Y-%m-%d %H:%M")
            if TZ:
                t = t.replace(tzinfo=TZ)
            else:
                t = t + dt.timedelta(hours=8)
            fresh_min = (now_cn() - t).total_seconds() / 60.0
        except Exception:
            pass
        return {"code": code, "name": name, "gsz": gsz, "pct": pct, "gztime": gztime, "fresh_min": fresh_min}
    except Exception:
        return None

# =========================
# 基金盘中预警配置（可按只覆盖）
# =========================
FUND_INTRADAY = os.getenv("FUND_INTRADAY", "false").lower() == "true"
FUND_EST_FRESH_SEC = int(os.getenv("FUND_EST_FRESH_SEC", "1500"))  # 25分钟
FUND_CONFIRM_ROUNDS = int(os.getenv("FUND_CONFIRM_ROUNDS", "2"))
FUND_COOLDOWN_MIN = int(os.getenv("FUND_COOLDOWN_MIN", "60"))
FUND_BUY_RATIO = _to_float(os.getenv("FUND_BUY_RATIO", "0.10"), 0.10)

def _load_proxy_map() -> Dict[str, List[str]]:
    raw = os.getenv("FUND_PROXY_MAP", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        # 兼容简单格式："006502:sz159995;022364:sh515000,sz159915"
        mp = {}
        for seg in raw.split(";"):
            if not seg.strip():
                continue
            k, v = seg.split(":", 1)
            mp[k.strip()] = [x.strip() for x in v.split(",") if x.strip()]
        return mp

FUND_PROXY_MAP = _load_proxy_map()

# 你可以直接改这里的默认 watch（名称校验、回撤区间、代理 ETF、新鲜度阈值（分钟））
FUND_WATCH = {
    "022364": {"expect_name": "永赢科技",     "pullback_band": [-4.0, -2.0], "etf": None,        "fresh_min": 25},
    "006502": {"expect_name": "财通集成电路", "pullback_band": [-4.0, -2.0], "etf": "sz159995", "fresh_min": 25},
    "018956": {"expect_name": "中航机遇领航", "pullback_band": [-4.0, -2.0], "etf": "sh512810", "fresh_min": 25},
}

def in_pullback_band(pct: float, band: Tuple[float, float]) -> bool:
    low, high = min(band), max(band)
    return (pct >= low) and (pct <= high)

def same_direction(fund_pct: float, etf_pct: Optional[float]) -> bool:
    if etf_pct is None:
        return True
    if abs(fund_pct) < 1e-6:
        return True
    return (fund_pct > 0 and etf_pct > 0) or (fund_pct < 0 and etf_pct < 0) or (abs(etf_pct) < 0.05)

# =========================
# ETF 波段配置
# =========================
ETF_INTRADAY = os.getenv("ETF_INTRADAY", "true").lower() == "true"
WATCH_ETF = [x.strip() for x in os.getenv("WATCH_ETF", "512810").split(",") if x.strip()]
DRAW_MIN = _to_float(os.getenv("DRAW_MIN", "-0.08"), -0.08) * 100.0  # 转为 %
DRAW_MAX = _to_float(os.getenv("DRAW_MAX", "-0.05"), -0.05) * 100.0
VOL_MULT = _to_float(os.getenv("VOL_MULT", "1.8"), 1.8)
ETF_COOLDOWN_MIN = int(os.getenv("ETF_COOLDOWN_MIN", "30"))
ETF_SLICE = _to_float(os.getenv("ETF_SLICE", "0.035"), 0.035)  # 建议买入 = 总资产*0.10*ETF_SLICE

# =========================
# 主逻辑：基金盘中预警
# =========================
def fund_intraday_check(state: dict):
    if not FUND_INTRADAY:
        return []

    cn = now_cn()
    if not within_trading_time(cn):
        print("[FUND] 非交易时段，跳过。")
        return []

    pending = state.setdefault("fund_pending", {})   # 连续确认计数
    cooldown = state.setdefault("fund_cooldown", {}) # 冷却截止时间戳
    fired_today = state.setdefault("fired", {}).setdefault(cn.date().isoformat(), [])

    results = []
    hits = []

    for code, cfg in FUND_WATCH.items():
        expect_name = cfg.get("expect_name")
        band = tuple(cfg.get("pullback_band", [-4.0, -2.0]))
        fresh_limit = int(cfg.get("fresh_min", 25))
        proxies = FUND_PROXY_MAP.get(code, [])
        etf_code = cfg.get("etf")
        if etf_code and etf_code not in proxies:
            proxies = [etf_code] + proxies

        # 估值
        est = fetch_fund_estimate(code, expected_name_substr=expect_name)
        if not est:
            results.append((code, "no_est_or_name_mismatch"))
            continue

        # 新鲜度
        if est["fresh_min"] * 60.0 > FUND_EST_FRESH_SEC or est["fresh_min"] > fresh_limit:
            results.append((code, f"stale({est['fresh_min']:.1f}m)"))
            continue

        # 代理ETF方向
        proxy_pct = None
        if proxies:
            pcts = []
            for p in proxies:
                q = fetch_sina_quote(p)
                if q:
                    pcts.append(q["pct"])
            if pcts:
                proxy_pct = sum(pcts)/len(pcts)

        trig_pb = in_pullback_band(est["pct"], band)
        ok_dir = same_direction(est["pct"], proxy_pct)

        # 连续确认 & 冷却
        key = f"fund:{code}"
        if time.time() < cooldown.get(key, 0):
            results.append((code, "cooldown"))
            continue

        if trig_pb and ok_dir:
            node = pending.setdefault(key, {"cnt": 0})
            node["cnt"] += 1
            if node["cnt"] >= FUND_CONFIRM_ROUNDS:
                tag = f"FUND:{code}:confirm"
                if tag not in fired_today:
                    hits.append({
                        "code": code, "name": est["name"], "pct": est["pct"], "gsz": est["gsz"],
                        "gztime": est["gztime"], "fresh_min": est["fresh_min"],
                        "proxy_pct": proxy_pct, "band": band
                    })
                    fired_today.append(tag)
                    cooldown[key] = time.time() + FUND_COOLDOWN_MIN * 60
                node["cnt"] = 0  # 重置
            else:
                results.append((code, f"confirming({node['cnt']}/{FUND_CONFIRM_ROUNDS})"))
        else:
            pending.setdefault(key, {"cnt": 0})
            pending[key]["cnt"] = 0
            results.append((code, f"nohit_pb={trig_pb},dir={ok_dir}"))

    # 推送（等权分配 进攻仓10%）
    if hits:
        attack_money = TOTAL_ASSETS * 0.10
        per_buy = attack_money / max(1, len(hits))
        title = f"BUY x{len(hits)}（场外预警｜进攻仓10%等权）"
        lines = [f"总资产≈¥{TOTAL_ASSETS:,.0f}，进攻仓≈¥{attack_money:,.0f}，单只≈¥{per_buy:,.0f}"]
        for r in hits:
            lines.append(
                f"BUY {r['code']} {r['name']}｜估值{r['pct']:+.2f}%｜估值≈{r['gsz']:.4f}｜"
                f"新鲜{r['fresh_min']:.1f}m｜ETF均{'' if r['proxy_pct'] is None else f'{r['proxy_pct']:+.2f}%'}｜"
                f"区间{r['band'][0]}~{r['band'][1]}"
            )
        serverchan_push(title, "\n".join(lines))
    else:
        # 打印简报
        if results:
            info = "；".join([f"{c}:{reason}" for c, reason in results])
            print(f"[FUND] {info}")

    return hits

# =========================
# 主逻辑：ETF 波段
# =========================
def etf_intraday_check(state: dict):
    if not ETF_INTRADAY:
        return []

    cn = now_cn()
    if not within_trading_time(cn):
        print("[ETF] 非交易时段，跳过。")
        return []

    cooldown = state.setdefault("etf_cooldown", {})
    fired_today = state.setdefault("fired", {}).setdefault(cn.date().isoformat(), [])

    hits = []
    for code in WATCH_ETF:
        q = fetch_sina_quote(code)
        if not q or q["price"] <= 0 or q["preclose"] <= 0:
            print(f"[ETF] {code} quote missing")
            continue

        # 参考高点：优先环境变量 REF_HIGH_xxx，没有则用当日 high（精度较差，建议配环境）
        ref_env = os.getenv(f"REF_HIGH_{code}", "")
        ref_high = _to_float(ref_env, 0.0)
        if ref_high <= 0:
            ref_high = q["high"] if q["high"] > 0 else q["price"]

        pullback = (q["price"] / ref_high - 1.0) * 100.0  # %
        in_zone = (pullback >= DRAW_MIN) and (pullback <= DRAW_MAX)

        # 放量近似过滤：用“总量/已过分钟均量”的倍数近似（无分钟历史时的折衷）
        mins = max(1, minutes_since_open(cn))
        avg_per_min = q["volume_hand"] / mins if mins > 0 else 0
        vol_ok = False
        try:
            # 近似判断：如果“总量 / (均量*分钟)” > VOL_MULT 判为放量（仅近似）
            vol_ok = avg_per_min > 0 and (q["volume_hand"] / (avg_per_min * mins)) > VOL_MULT
        except Exception:
            vol_ok = False

        key = f"etf:{code}"
        if time.time() < cooldown.get(key, 0):
            print(f"[ETF] {code} cooldown")
            continue

        if in_zone or vol_ok:
            tag = f"ETF:{code}:wave"
            if tag not in fired_today:
                # 资金建议：总资产 * 10% * ETF_SLICE
                buy_amt = TOTAL_ASSETS * 0.10 * ETF_SLICE
                reason = []
                if in_zone:
                    reason.append(f"回撤{pullback:.2f}%∈[{DRAW_MIN:.1f}%,{DRAW_MAX:.1f}%]")
                if vol_ok:
                    reason.append(f"放量≈>{VOL_MULT:.1f}x(近似)")
                title = f"BUY {code} {q['name']}"
                body = (
                    f"价格:{q['price']:.3f}（当日{q['pct']:+.2f}%）\n"
                    f"参考高点:{ref_high:.3f}；{';'.join(reason)}\n"
                    f"建议买入≈¥{buy_amt:,.0f}（进攻仓切片 ETF_SLICE={ETF_SLICE:.3f}）"
                )
                serverchan_push(title, body)
                fired_today.append(tag)
                cooldown[key] = time.time() + ETF_COOLDOWN_MIN * 60

                hits.append({"code": code, "name": q["name"], "price": q["price"], "pullback": pullback, "vol_ok": vol_ok})
        else:
            print(f"[ETF] {code} nohit pullback={pullback:.2f}% vol_ok={vol_ok}")

    return hits

# =========================
# 主入口
# =========================
def main():
    st = load_state()

    cn = now_cn()
    if not within_trading_time(cn):
        print("[INFO] 非交易时段。INTRADAY_ONLY=1 时不会触发任何信号。")

    # 先跑 ETF，再跑基金
    etf_hits = etf_intraday_check(st)
    fund_hits = fund_intraday_check(st)

    save_state(st)

    # 控制台汇总
    if not etf_hits and not fund_hits:
        print("[DONE] no triggers.")
    else:
        print(f"[DONE] etf_hits={len(etf_hits)} fund_hits={len(fund_hits)}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[ERROR]", repr(e))
        # 不抛出，避免打断外层循环
