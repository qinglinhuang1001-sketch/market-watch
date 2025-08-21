#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import csv
import time
import random
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ==== 基础配置 ====
TOKYO = timezone(timedelta(hours=9))
TODAY = datetime.now(TOKYO).strftime("%Y-%m-%d")

OUT_DIR = os.path.join("reports", "nav")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_CSV = os.path.join(OUT_DIR, f"{TODAY}.csv")

FUNDS = [
    {"code": "022364", "name": "永赢科技智选A"},
    {"code": "006502", "name": "财通集成电路A"},
    {"code": "018956", "name": "中航机遇领航A"},
]

# Eastmoney/Jijin 需要 UA/Referer，否则容易 403
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://fundf10.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

class FetchError(Exception):
    pass

def _session_with_headers() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    return sess

@retry(
    retry=retry_if_exception_type(FetchError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=6),
)
def fetch_fund_once(sess: requests.Session, code: str) -> dict:
    """
    读取基金当日估值（或最新净值）。优先估值接口，不行再用净值页兜底。
    这里只实现一个可用方案；如果后续你有更稳定的内部 API，可以替换这个函数。
    返回: {"date": "YYYY-MM-DD", "value": float, "pct": float, "source": "xxx_api"}
    """
    # 方案 A：使用天天基金估值接口（需要 UA/Referer；字段逻辑随官网可能调整）
    # 这里用的是一个公开的估值接口示例，注意：此类接口经常会调整/风控，403 很常见
    # 所以加了重试与备用方案。
    try:
        # 示例接口（仅示意）：https://fundgz.1234567.com.cn/js/xxxx.js
        # 真实使用时可替换成你当前在仓库里已验证可用的那套请求与解析
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        resp = sess.get(url, timeout=8)
        if resp.status_code == 200 and "jsonpgz" in resp.text:
            # 简单解析：jsonpgz({"fundcode":"xxxx","name":"...","jzrq":"2025-08-19","dwjz":"2.5101","gsz":"2.5231","gszzl":"0.52","gztime":"2025-08-19 15:00"})
            txt = resp.text.strip()
            data_part = txt[txt.find("(") + 1 : txt.rfind(")")]
            obj = pd.read_json(data_part, typ="series")
            date = str(obj.get("jzrq") or TODAY)
            value = float(obj.get("dwjz"))  # 单位净值
            pct = float(obj.get("gszzl") or 0.0) / 100.0  # 估算涨跌幅（百分比->小数）
            return {"date": date, "value": value, "pct": pct, "source": "eastmoney_gz"}
        else:
            raise FetchError(f"gz_resp={resp.status_code}")
    except Exception as e:
        # 方案 B：兜底（净值历史/详情页 JSON）
        # 这里给一个可替的示意接口，你之前仓库中实际可用的 API 建议继续用。
        # 注意兜底方案不一定有当日估值，只能拿到最近净值。
        try:
            url2 = f"https://api.doctorxiong.club/v1/fund?code={code}"
            r2 = sess.get(url2, timeout=8)
            if r2.status_code == 200:
                js = r2.json()
                if js.get("code") == 200 and js.get("data"):
                    item = js["data"][0]
                    value = float(item["netWorth"])
                    date = str(item["netWorthDate"])
                    # pct 这里可能没有，设为 0
                    return {"date": date, "value": value, "pct": 0.0, "source": "doctorxiong_api"}
            raise FetchError(f"fallback_resp={r2.status_code}, err={e}")
        except Exception:
            raise FetchError(f"fund {code} all endpoints failed")

def main():
    rows = []
    sess = _session_with_headers()

    for f in FUNDS:
        code = f["code"]
        name = f["name"]
        time.sleep(random.uniform(0.3, 0.9))  # 轻微打散，降低风控
        try:
            info = fetch_fund_once(sess, code)
            rows.append(
                {
                    "type": "FUND",
                    "code": code,
                    "name": name,
                    "date": info["date"],
                    "value": info["value"],
                    "pct": info["pct"],
                    "source": info["source"],
                }
            )
        except Exception as e:
            rows.append(
                {
                    "type": "FUND",
                    "code": code,
                    "name": name,
                    "date": TODAY,
                    "value": "",
                    "pct": "",
                    "source": "fetch_error",
                }
            )

    # 写出 CSV
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp, fieldnames=["type", "code", "name", "date", "value", "pct", "source"]
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"[OK] wrote {OUT_CSV} ({len(rows)} rows)")

if __name__ == "__main__":
    main()
