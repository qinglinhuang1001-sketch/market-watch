#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import csv, os, datetime as dt, json, pathlib, requests

SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY","").strip()
LOG_FILE = "logs/signals.csv"
NAV_DIR  = "reports/nav"
REPORT_DIR = "reports/daily"

def bj_today(): return (dt.datetime.utcnow()+dt.timedelta(hours=8)).date()
def today_str(): return str(bj_today())

def load_today_signals():
    rows=[]
    if not os.path.exists(LOG_FILE): return rows
    with open(LOG_FILE,"r",encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date")==today_str():
                rows.append(row)
    return rows

def load_today_nav():
    path = os.path.join(NAV_DIR, f"{today_str()}.csv")
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(row)
    return out

def write_markdown(sig_rows, nav_rows):
    pathlib.Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
    path=os.path.join(REPORT_DIR, f"{today_str()}.md")
    lines=[f"# {today_str()} 交易日信号日报\n",
           f"- 触发总数：**{len(sig_rows)}**\n",
           ""]
    # 信号表
    if sig_rows:
        lines += [
            "| 时间 | 类型 | 代码 | 名称 | 信号 | 理由 | 价格/估值 | 当日涨跌 | 距参高回撤 | 建议规模(手/元) | 参数 |",
            "|---|---|---|---|---|---|---:|---:|---:|---:|---|",
        ]
        for r in sig_rows:
            lines.append(
                f"| {r['time']} | {r['asset_type']} | {r['code']} | {r['name']} | {r['signal']} | {r['reason']} "
                f"| {r.get('price_or_nav','')} | {r.get('day_pct','')} | {r.get('from_high_pct','')} "
                f"| {r.get('size_lots','') or r.get('size_amount','')} | {r.get('params','')} |"
            )
        lines.append("")
    else:
        lines.append("> 当日无触发。")
        lines.append("")

    # 官方净值/收盘价板块
    if nav_rows:
        lines += [
            "## 官方净值 / 收盘价",
            "| 类型 | 代码 | 名称 | 日期 | 净值/收盘 | 当日涨跌(%) | 来源 |",
            "|---|---|---|---|---:|---:|---|",
        ]
        for r in nav_rows:
            lines.append(
                f"| {r['type']} | {r['code']} | {r['name']} | {r['date']} | {r['value']} | {r.get('pct','')} | {r['source']} |"
            )
    open(path,"w",encoding="utf-8").write("\n".join(lines))
    return path

def push_summary(sig_rows, nav_rows):
    if not SERVER_CHAN_KEY: return
    title=f"日报 | {today_str()} 信号{len(sig_rows)}条 / NAV {len(nav_rows)}条"
    if not sig_rows:
        desp="当日无信号触发。"
    else:
        tops=sig_rows[:3]
        lines=[f"- {r['time']} {r['code']} {r['name']} | {r['signal']}/{r['reason']}" for r in tops]
        if len(sig_rows)>3: lines.append(f"...共 {len(sig_rows)} 条")
        desp="\n".join(lines)
    try:
        requests.post(f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send",
                      data={"title":title,"desp":desp},timeout=10)
    except Exception:
        pass

def main():
    sig_rows = load_today_signals()
    nav_rows = load_today_nav()
    path = write_markdown(sig_rows, nav_rows)
    push_summary(sig_rows, nav_rows)
    print("saved:", path)

if __name__=="__main__":
    main()
