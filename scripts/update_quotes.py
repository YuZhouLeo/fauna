#!/usr/bin/env python3
"""每日更新 index.html 裡 PAYLOAD 的成交量、收盤價與 N 日均量。

只覆寫每檔的 v(當日張數) / va(N 日均量) / p(收盤價) 與資料日期，
公司名 n / 產業 i / 市場 m / 業務描述 d 一律沿用舊值。
描述另可放 data/descriptions.json（{"2330": "..."}），優先權高於 payload 內的 d。

資料來源與端點見 scripts/twquotes.py。
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twquotes as tq  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
DESC_FILE = os.path.join(ROOT, "data", "descriptions.json")

TPE = timezone(timedelta(hours=8))
AVG_DAYS = 20      # 均量取幾個交易日
MIN_AVG_DAYS = 5   # 少於這麼多天就不寫 va，讓前端退回用當日量


def main():
    src, m, payload = tq.read_payload(INDEX)
    old = {s["c"]: s for s in payload["stocks"]}

    print("抓行情…")
    now = datetime.now(TPE)
    tw_date, tw_q, tp_date, tp_q = None, {}, None, {}
    for day in tq.recent_trading_days(now, back=8):  # 假日往前找最近的交易日
        tw_date, tw_q, tp_date, tp_q = tq.quotes_for(day)
        if tw_q or tp_q:
            break
    print(f"  上市 {tw_date} {len(tw_q)} 筆 / 上櫃 {tp_date} {len(tp_q)} 筆")
    if not tw_q and not tp_q:
        raise SystemExit("兩邊行情都沒抓到，中止")

    print("抓上市櫃名單…")
    listed, otc = tq.universes()
    tw_ind = tq.industry_map(listed, old, "上市")
    tp_ind = tq.industry_map(otc, old, "上櫃")
    print(f"  上市 {len(listed)} 家 / 上櫃 {len(otc)} 家")

    descs = {}
    if os.path.exists(DESC_FILE):
        descs = json.load(open(DESC_FILE, encoding="utf-8"))
        if descs:
            print(f"  descriptions.json {len(descs)} 筆")

    stocks, added, stale = [], [], 0
    for market, uni, quotes, indmap in (("上市", listed, tw_q, tw_ind),
                                        ("上櫃", otc, tp_q, tp_ind)):
        for code in sorted(uni):
            ind_code, abbrev = uni[code]
            prev, q = old.get(code), quotes.get(code)
            if q is None and prev is None:
                continue  # 名單有、但沒行情也沒舊資料（多半是掛牌了還沒開始交易）
            if prev is None and tq.is_dr(code, abbrev):
                continue
            if q is None:
                # 這輪沒抓到這檔（停牌，或整個市場 feed 掛了）→ 沿用舊值
                v, p = prev.get("v", 0), prev.get("p", "---")
                stale += 1
            else:
                v, p = q
                v = 0 if v is None else v
                # 當日無成交 → 量記 0，價沿用上次有成交的收盤價
                p = p or (prev or {}).get("p") or "---"

            name = (prev or {}).get("n") or abbrev or code
            if prev is None:
                added.append(f"{code} {name}")

            s = {"c": code, "n": name,
                 "i": (prev or {}).get("i") or indmap.get(ind_code, ""),
                 "m": market, "v": v, "p": p}
            d = descs.get(code) or (prev or {}).get("d")
            if d:
                s["d"] = d
            stocks.append(s)

    # 安全閥：這是無人看管的每日排程，名單 API 若回傳殘缺資料，
    # 不該讓牌組被砍掉一大塊。下降超過 5% 就中止，寧可今天不更新。
    if len(stocks) < len(old) * 0.95:
        raise SystemExit(
            f"中止：檔數從 {len(old)} 掉到 {len(stocks)}（少了 {len(old) - len(stocks)} 檔，>5%），"
            "多半是上市櫃名單 API 回傳不完整，今天不更新。")

    dropped = [c for c in old if c not in {s["c"] for s in stocks}]
    stocks.sort(key=lambda s: s["c"])

    # 先把今天的量寫進歷史，再算均量（今天要算進去）
    day_key = tw_date or tp_date
    tq.save_day(ROOT, day_key, {s["c"]: s["v"] for s in stocks})
    avg, ndays = tq.load_history(ROOT, days=AVG_DAYS)
    if ndays >= MIN_AVG_DAYS:
        for s in stocks:
            if s["c"] in avg:
                s["va"] = avg[s["c"]]
        print(f"  {ndays} 日均量已寫入 {sum('va' in s for s in stocks)} 檔")
    else:
        print(f"  歷史只有 {ndays} 天（<{MIN_AVG_DAYS}），這輪不寫均量，"
              f"前端會退回用當日量。要補跑：python3 scripts/backfill_volume.py")

    payload = {"date_twse": tw_date or payload.get("date_twse"),
               "date_tpex": tp_date or payload.get("date_tpex"),
               "avg_days": ndays if ndays >= MIN_AVG_DAYS else 0,
               "stocks": stocks}

    print(f"\n共 {len(stocks)} 檔｜新增 {len(added)}｜移除 {len(dropped)}｜沿用舊值 {stale}")
    if added:
        print("  新增:", ", ".join(added[:20]))
    if dropped:
        print("  移除:", ", ".join(dropped[:20]))

    if tq.write_payload(INDEX, src, m, payload):
        print("已更新 index.html")
    else:
        print("內容沒變，不寫檔（workflow 會靠 git diff 自己跳過 commit）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
