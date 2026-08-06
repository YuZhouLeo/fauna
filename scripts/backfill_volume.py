#!/usr/bin/env python3
"""回補最近 N 個交易日的成交量到 data/volume/，讓均量篩選一上線就有資料。

    python3 scripts/backfill_volume.py [天數，預設 20]

平常不用跑，只有第一次啟用均量篩選、或歷史檔被清掉時才需要。
每個請求間隔 0.7 秒，回補 20 天大約要 1 分鐘。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twquotes as tq  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPE = timezone(timedelta(hours=8))


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    have = set()
    d = tq.hist_dir(ROOT)
    if os.path.isdir(d):
        have = {f[:-5] for f in os.listdir(d) if f.endswith(".json")}
    print(f"目標 {want} 個交易日，已有 {len(have)} 個")

    got = 0
    day = datetime.now(TPE)
    # 往回找的日曆天要留餘裕：假日 + 連假，抓 2.2 倍再加 10 天
    for _ in range(int(want * 2.2) + 10):
        if got >= want:
            break
        roc = tq.to_roc(day.strftime("%Y%m%d"))
        if roc in have:
            print(f"  {roc} 已有，跳過")
            got += 1
            day -= timedelta(days=1)
            continue

        merged = {}
        tw_d, tw_q = tq.twse_quotes(day)
        for code, (v, _) in tw_q.items():
            if len(code) == 4 and code.isdigit():
                merged[code] = v or 0
        tp_d, tp_q = tq.tpex_quotes(day)
        for code, (v, _) in tp_q.items():
            if len(code) == 4 and code.isdigit():
                merged[code] = v or 0

        if merged:
            tq.save_day(ROOT, tw_d or tp_d or roc, merged)
            got += 1
            print(f"  {tw_d or tp_d}  上市 {len(tw_q)} / 上櫃 {len(tp_q)} → 存 {len(merged)} 檔")
        else:
            print(f"  {day:%Y-%m-%d} 非交易日")
        day -= timedelta(days=1)

    avg, ndays = tq.load_history(ROOT, days=want)
    print(f"\n完成：{ndays} 個交易日、{len(avg)} 檔有均量")
    return 0


if __name__ == "__main__":
    sys.exit(main())
