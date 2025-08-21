# -*- coding: utf-8 -*-
"""
fund_selector.py
每周动态筛选基金：采集 -> 特征计算 -> 综合打分 -> 输出排名与调仓建议

依赖:
  - requests
  - pandas
  - numpy

配置:
  - selector_config.yml （可选）：
      candidates: [ "022364", "006502", "018956", "159915", "512810", ... ]
      overrides:
        "022364":
          manager_years: 1.0
          fee_total: 1.2       # 管理+托管的年费率（%）
          scale_bil: 10.5      # 份额规模（亿元）
          theme: "科技/通信"
        "006502":
          manager_years: 3.5
          fee_total: 1.5
          scale_bil: 20.0
          theme: "半导体"
  - 环境变量（可选）：
      TARGET_RETURN=0.20    # 年底目标收益 20%
      MAX_DD=0.10           # 组合最大回撤容忍 10%

输出:
  - reports/weekly/YYYY-MM-DD-fund-ranking.csv
  - reports/weekly/YYYY-MM-DD-fund-report.md
"""

import os
import re
import json
import math
import time
import yaml
import errno
import random
import datetime as dt
from typing import Dict, Any, List, Optional

import requests
import numpy as np
import pandas as pd

# ---------------------------
# 基本设置
# ---------------------------
ROOT = os.getcwd()
OUT_DIR = os.path.join(ROOT, "reports", "weekly")
os.makedirs(OUT_DIR, exist_ok=True)
TODAY = dt.date.today().isoformat()

TARGET_RETURN = float(os.getenv("TARGET_RETURN", "0.20"))
MAX_DD = float(os.getenv("MAX_DD", "0.10"))

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

S = requests.Session()
S.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"})
S.timeout = 8

CONFIG_FILE = "selector_config.yml"

DEFAULT_POOL = [
    # 你可以先用这批跑通，再按需在 selector_config.yml 里改 candidates
    "022364",  # 永赢科技智选A（场外）
    "006502",  # 财通集成电路A（场外）
    "018956",  # 中航机遇领航A（场外）
    "159915",  # 易方达创业板ETF（场内示例）
    "512810",  # 国防军工ETF（场内示例）
]

# ---------------------------
# 采集：DoctorXiong API（免费接口，字段稳定、速率友好）
#   https://www.doctorxiong.club/api/#/fund/getV1Fund
#   /v1/fund?code=xxxx
#   常见字段：name, netWorthDate, dayGrowth, lastWeekGrowth, lastMonthGrowth, lastThreeMonthsGrowth,
#            lastSixMonthsGrowth, lastYearGrowth, netWorth, expectWorth, expectGrowth, manager, fundScale
# ---------------------------

def dx_fund_basic(code: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.doctorxiong.club/v1/fund?code={code}"
    try:
        r = S.get(url, timeout=8)
        if r.status_code != 200:
            return None
        js = r.json()
        if js.get("code") != 200 or not js.get("data"):
            return None
        item = js["data"][0]
        return item
    except Exception:
        return None

# 备份采集（东财 F10 简略页），仅拿名称，避免完全拿不到
def eastmoney_name_only(code: str) -> Optional[str]:
    try:
        # 很多 F10 页是 HTML；这里只用来兜底拿名字，不做复杂解析
        url = f"https://fund.eastmoney.com/{code}.html"
        r = S.get(url, timeout=8, headers={"Referer": "https://fund.eastmoney.com"})
        if r.status_code != 200:
            return None
        m = re.search(r"<title>([^<]+?)\(", r.text)
        if not m:
            return None
        name = m.group(1).strip()
        return name
    except Exception:
        return None

# ---------------------------
# 配置与覆盖
# ---------------------------
def load_config() -> Dict[str, Any]:
    cfg = {"candidates": DEFAULT_POOL, "overrides": {}}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
            user_cfg = yaml.safe_load(fp) or {}
            cfg.update({k:v for k,v in user_cfg.items() if k in ("candidates","overrides")})
    return cfg

# ---------------------------
# 特征工程：把采集到的数据标准化为打分所需字段
# ---------------------------
def feature_row(code: str, dx: Optional[Dict[str, Any]], overrides: Dict[str, Any]) -> Dict[str, Any]:
    # 默认值
    name = (dx or {}).get("name") or eastmoney_name_only(code) or code
    # 近区间收益（%）
    w1  = _to_float((dx or {}).get("lastWeekGrowth"))
    m1  = _to_float((dx or {}).get("lastMonthGrowth"))
    m3  = _to_float((dx or {}).get("lastThreeMonthsGrowth"))
    m6  = _to_float((dx or {}).get("lastSixMonthsGrowth"))
    y1  = _to_float((dx or {}).get("lastYearGrowth"))
    day = _to_float((dx or {}).get("dayGrowth"))

    # 规模（亿元）
    # doctorxiong 的 fundScale 字段可能为 "10.23亿"，做个解析；优先 overrides
    scale_bil = overrides.get("scale_bil")
    if scale_bil is None:
        raw_scale = (dx or {}).get("fundScale") or ""
        scale_bil = parse_scale_bil(raw_scale)

    # 费率（管理+托管，%）——多数接口不直接给；建议你在 overrides 里维护
    fee_total = overrides.get("fee_total", None)

    # 经理年限（年）——建议在 overrides 维护（接口抓取不稳定）
    manager_years = overrides.get("manager_years", None)

    theme = overrides.get("theme", "")  # 行业主题标签（手工标注）

    return {
        "code": code, "name": name, "theme": theme,
        "w1": w1, "m1": m1, "m3": m3, "m6": m6, "y1": y1, "day": day,
        "scale_bil": scale_bil, "fee_total": fee_total, "manager_years": manager_years
    }

def _to_float(x, default=None):
    try:
        if x is None: return default
        s = str(x).strip().replace("%","").replace(",","")
        return float(s)
    except Exception:
        return default

def parse_scale_bil(raw: str) -> Optional[float]:
    """
    原始: "10.23亿", "2.5万万", "—"
    返回: 以 亿元为单位的 float
    """
    if not raw: return None
    s = str(raw)
    m = re.search(r"([\d\.]+)", s)
    if not m: return None
    val = float(m.group(1))
    if "万亿" in s:
        return val * 1e4  # 万亿 -> 亿
    if "亿" in s:
        return val
    if "万万" in s:
        return val  # 万万 ~ 亿（有些页面这么写）
    return val

# ---------------------------
# 打分：四大维度（经理/产品/历史/行业前景）
#   总分100：经理20、产品20、历史30、前景30
# ---------------------------
def score_row(r: Dict[str, Any]) -> Dict[str, Any]:
    # 经理（20）
    sc_mgr = score_manager(r.get("manager_years"))

    # 产品（20）——规模5~50亿最优；费率越低越好；规模缺失不给分惩罚
    sc_prod = score_product(r.get("scale_bil"), r.get("fee_total"))

    # 历史（30）——近1月/3月/6月/1年的收益、波动近似（用回撤代理）
    sc_hist = score_history(r)

    # 行业前景（30）——用近1周/近1月表现的“强度”做景气近似（缺行业指数时的可行代理）
    sc_outlook = score_outlook(r)

    total = sc_mgr + sc_prod + sc_hist + sc_outlook

    return {
        **r,
        "score_mgr": sc_mgr,
        "score_prod": sc_prod,
        "score_hist": sc_hist,
        "score_outlook": sc_outlook,
        "score_total": round(total, 2),
    }

def score_manager(years: Optional[float]) -> float:
    if years is None:
        return 8.0  # 缺数据给一个中性偏低
    # 0-1年:10；1-3年:14；3-5年:17；5年以上:20
    if years < 1: return 10.0
    if years < 3: return 14.0
    if years < 5: return 17.0
    return 20.0

def score_product(scale_bil: Optional[float], fee_total: Optional[float]) -> float:
    # 规模（12分）：5~50亿最优；<2或>100给低分
    sc_scale = 6.0
    if scale_bil is not None:
        if 5 <= scale_bil <= 50: sc_scale = 12.0
        elif 2 <= scale_bil < 5: sc_scale = 9.0
        elif 50 < scale_bil <= 100: sc_scale = 8.0
        else: sc_scale = 4.0
    # 费率（8分）：<=1.0% 给满；1.0~1.5 给6；>1.5 给3；缺失给5
    if fee_total is None:
        sc_fee = 5.0
    else:
        if fee_total <= 1.0: sc_fee = 8.0
        elif fee_total <= 1.5: sc_fee = 6.0
        else: sc_fee = 3.0
    return sc_scale + sc_fee

def score_history(r: Dict[str, Any]) -> float:
    # 历史（30）：m1 10分、m3 10分、m6 6分、y1 4分；负收益按 0 计
    def bucket(val, full):
        if val is None: return 0.4 * full  # 缺数据给 40% 分
        if val <= 0: return 0.2 * full
        # 线性压缩：20% 给满
        return min(full, full * (val / 20.0))
    sc = 0.0
    sc += bucket(r.get("m1"), 10.0)
    sc += bucket(r.get("m3"), 10.0)
    sc += bucket(r.get("m6"), 6.0)
    sc += bucket(r.get("y1"), 4.0)
    return sc

def score_outlook(r: Dict[str, Any]) -> float:
    # 前景（30）：用近1周(w1)和近1月(m1)的“强度”近似行业景气；用 day 作为短期热度微调
    w1 = r.get("w1"); m1 = r.get("m1"); day = r.get("day")
    def bucket(val, full):
        if val is None: return 0.4 * full
        # 15% 给满
        return max(0.0, min(full, full * (val / 15.0)))
    sc = 0.0
    sc += bucket(w1, 12.0)
    sc += bucket(m1, 14.0)
    # 日内热度微调：±2分范围
    if day is None:
        sc += 0.8
    else:
        sc += max(-2.0, min(2.0, day / 2.0))
    return sc

# ---------------------------
# 主流程
# ---------------------------
def main():
    cfg = load_config()
    pool: List[str] = list(dict.fromkeys(cfg.get("candidates") or DEFAULT_POOL))
    overrides: Dict[str, Any] = cfg.get("overrides") or {}

    rows = []
    for code in pool:
        dx = dx_fund_basic(code)
        feat = feature_row(code, dx, overrides.get(code, {}))
        rows.append(feat)
        time.sleep(random.uniform(0.15, 0.4))  # 轻微限速

    df = pd.DataFrame(rows)
    scored = df.apply(lambda r: pd.Series(score_row(r.to_dict())), axis=1)
    ranked = scored.sort_values("score_total", ascending=False).reset_index(drop=True)

    # 输出 CSV
    csv_path = os.path.join(OUT_DIR, f"{TODAY}-fund-ranking.csv")
    ranked.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # 输出 MD（简洁，不在表格里放长句）
    md_path = os.path.join(OUT_DIR, f"{TODAY}-fund-report.md")
    with open(md_path, "w", encoding="utf-8") as fp:
        fp.write(f"# 每周基金动态筛选（{TODAY}）\n\n")
        fp.write(f"- 目标: 年底总收益 ≥ {int(TARGET_RETURN*100)}%\n")
        fp.write(f"- 风险: 组合最大回撤 ≤ {int(MAX_DD*100)}%\n")
        fp.write(f"- 候选数量: {len(pool)}\n\n")
        fp.write("## Top 10 综合评分\n\n")
        top = ranked.head(10)[
            ["code","name","theme","score_total","score_mgr","score_prod","score_hist","score_outlook",
             "w1","m1","m3","m6","y1","scale_bil","fee_total","manager_years"]
        ]
        fp.write(top.to_markdown(index=False))
        fp.write("\n\n")
        fp.write("## 组合建议（示例）\n\n")
        fp.write("- 取 Top 3–5 为持仓候选；单只≤25%；组合仓位 80–100%\n")
        fp.write("- 若持仓基金当周跌出前5，下周调出；单只止损 -5%，止盈 +15% 分批\n")

    print(f"[OK] wrote:\n  - {csv_path}\n  - {md_path}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[ERROR]", repr(e))
