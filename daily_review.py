#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import csv, os, datetime as dt, json, pathlib, requests

SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY","").strip()
LOG_FILE = "logs/signals.csv"
REPORT_DIR = "reports/daily"

def bj_today(): return (dt.datetime.utcnow()+dt.timedelta(hours=8)).date()
def load_today_signals():
    rows=[]
    if not os.path.exists(LOG_FILE): return rows
    today=str(bj_today())
    with open(LOG_FILE,"r",encoding="utf-8") as f:
        for i,row in enumerate(csv.DictReader(f)):
            if row.get("date")==today:
                rows.append(row)
    return rows

def write_markdown(rows):
    pathlib.Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
    path=os.path.join(REPORT_DIR, f"{bj_today()}.md")
    lines=[f"# {bj_today()} 交易日信号日报\n",
           f"- 触发总数：**{len(rows)}**\n",
           ""]
    if rows:
        lines.append("| 时间 | 类型 | 代码 | 名称 | 信号 | 理由 | 价格/估值 | 当日涨跌 | 距参高回撤 | 建议规模(手/元) | 参数 |")
        lines.append("|---|---|---|---|---|---|---:|---:|---:|---:|---|")
        for r in rows:
            lines.append(f"| {r['time']} | {r['asset_type']} | {r['code']} | {r['name']} | {r['signal']} | {r['reason']} "
                         f"| {r.get('price_or_nav','')} | {r.get('day_pct','')} | {r.get('from_high_pct','')} "
                         f"| {r.get('size_lots','') or r.get('size_amount','')} | {r.get('params','')} |")
    else:
        lines.append("> 当日无触发。")
    open(path,"w",encoding="utf-8").write("\n".join(lines))
    return path

def push_summary(rows):
    if not SERVER_CHAN_KEY: return
    title=f"日报 | {bj_today()} 触发{len(rows)}条"
    if not rows:
        desp="当日无触发。"
    else:
        # 只发精简摘要（前三条）
        tops=rows[:3]
        lines=[f"- {r['time']} {r['asset_type']} {r['code']} {r['name']} | {r['signal']}/{r['reason']}" for r in tops]
        if len(rows)>3: lines.append(f"...共 {len(rows)} 条，详见仓库 reports/daily/{bj_today()}.md")
        desp="\n".join(lines)
    try:
        requests.post(f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send",
                      data={"title":title,"desp":desp},timeout=10)
    except Exception:
        pass

def main():
    rows=load_today_signals()
    path=write_markdown(rows)
    push_summary(rows)
    print(f"saved: {path}")

if __name__=="__main__":
    main()
