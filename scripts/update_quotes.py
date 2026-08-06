#!/usr/bin/env python3
"""每日更新 index.html 裡 PAYLOAD 的成交量與收盤價。

資料來源（皆為官方公開資料，無需金鑰）:
  上市 行情  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX  (type=ALLBUT0999)
  上櫃 行情  https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
  上市 名單  https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  上櫃 名單  https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O

只覆寫每檔的 v(成交張數) / p(收盤價) 與兩個資料日期，
公司名稱 n / 產業 i / 市場 m / 業務描述 d 一律沿用舊值。
描述另可放在 data/descriptions.json（{"2330": "..."}），會覆蓋 payload 內的 d。

沒有任何一邊有新資料時 exit code 1，讓 workflow 跳過 commit。
"""

import json
import os
import re
import ssl
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
DESC_FILE = os.path.join(ROOT, "data", "descriptions.json")
HIST_DIR = os.path.join(ROOT, "data", "volume")
HIST_KEEP = 60  # 保留幾個交易日，供日後做均量篩選

TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (fauna twflip quote updater)"}


def _ssl_ctx():
    """macOS 內建 Python 常缺 root CA；有 certifi 就用，沒有就用系統預設。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


CTX = _ssl_ctx()


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 想吞掉所有網路層錯誤後重試
            last = e
            print(f"  取數失敗({i + 1}/{tries}) {url} :: {e}", file=sys.stderr)
    raise SystemExit(f"取數連續失敗: {url} :: {last}")


def num(s):
    """'20,385,198' -> 20385198；'--' / '' -> None"""
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s or s in {"--", "---", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def price(s):
    """收盤價保留原字串格式（'23.80'），無成交回 None。"""
    v = num(s)
    return None if v is None else f"{v:.2f}"


def lots(shares):
    """股 -> 張"""
    v = num(shares)
    return None if v is None else int(round(v / 1000))


# ---------- 行情 ----------

def twse_quotes(date_yyyymmdd):
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?date={date_yyyymmdd}&type=ALLBUT0999&response=json")
    d = fetch(url)
    if d.get("stat") != "OK":
        return None, {}
    table = None
    for t in d.get("tables", []):
        if "每日收盤行情" in t.get("title", ""):
            table = t
            break
    if not table:
        return None, {}
    f = table["fields"]
    ic, iv, ip = f.index("證券代號"), f.index("成交股數"), f.index("收盤價")
    out = {}
    for row in table["data"]:
        out[row[ic].strip()] = (lots(row[iv]), price(row[ip]))
    roc = d.get("date", date_yyyymmdd)  # '20260806'
    roc = f"{int(roc[:4]) - 1911:03d}{roc[4:]}"
    return roc, out


def tpex_quotes():
    d = fetch("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    out = {}
    date = None
    for row in d:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        date = date or str(row.get("Date", "")).strip()
        out[code] = (lots(row.get("TradingShares")), price(row.get("Close")))
    return date, out


# ---------- 名單 + 產業別 ----------

def universes():
    """回傳 {代號: (產業代碼, 官方簡稱)}，用來界定「哪些是真的上市/上櫃公司」（排除 ETF、權證）。"""
    tw = fetch("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    tp = fetch("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O")
    listed = {str(r["公司代號"]).strip():
              (str(r.get("產業別", "")).strip(), str(r.get("公司簡稱", "")).strip())
              for r in tw if len(str(r["公司代號"]).strip()) == 4}
    otc = {str(r["SecuritiesCompanyCode"]).strip():
           (str(r.get("SecuritiesIndustryCode", "")).strip(),
            str(r.get("CompanyAbbreviation", "")).strip())
           for r in tp if len(str(r["SecuritiesCompanyCode"]).strip()) == 4}
    return listed, otc


def is_dr(code, name):
    """台灣存託憑證（91xx / 名稱帶 -DR）：外國公司來台掛牌，不收進台股牌組。"""
    return name.endswith("-DR") or (code.startswith("91") and not name)


def industry_map(uni, stocks_by_code, market):
    """用現有 payload 的產業中文名反推「產業代碼 -> 中文名」，新上市櫃公司才有產業可填。"""
    tally = defaultdict(Counter)
    for code, (ind, _) in uni.items():
        s = stocks_by_code.get(code)
        if s and s.get("m") == market and s.get("i"):
            tally[ind][s["i"]] += 1
    return {k: v.most_common(1)[0][0] for k, v in tally.items()}


# ---------- 主流程 ----------

def main():
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(r"const PAYLOAD = (\{.*?\});", src, re.S)
    if not m:
        raise SystemExit("index.html 找不到 const PAYLOAD = {...};")
    payload = json.loads(m.group(1))
    old = {s["c"]: s for s in payload["stocks"]}

    print("抓行情…")
    now = datetime.now(TPE)
    tw_date, tw_q = None, {}
    for back in range(0, 8):  # 假日/補班往前找最近有資料的交易日
        d = (now - timedelta(days=back)).strftime("%Y%m%d")
        tw_date, tw_q = twse_quotes(d)
        if tw_q:
            break
    tp_date, tp_q = tpex_quotes()
    print(f"  上市 {tw_date} {len(tw_q)} 筆 / 上櫃 {tp_date} {len(tp_q)} 筆")
    if not tw_q and not tp_q:
        raise SystemExit("兩邊行情都沒抓到，中止")

    print("抓上市櫃名單…")
    listed, otc = universes()
    tw_ind = industry_map(listed, old, "上市")
    tp_ind = industry_map(otc, old, "上櫃")
    print(f"  上市 {len(listed)} 家 / 上櫃 {len(otc)} 家")

    descs = {}
    if os.path.exists(DESC_FILE):
        descs = json.load(open(DESC_FILE, encoding="utf-8"))
        print(f"  descriptions.json {len(descs)} 筆")

    stocks, added, stale = [], [], 0
    for market, uni, quotes, indmap in (("上市", listed, tw_q, tw_ind),
                                        ("上櫃", otc, tp_q, tp_ind)):
        for code in sorted(uni):
            ind_code, abbrev = uni[code]
            prev = old.get(code)
            q = quotes.get(code)
            if q is None and prev is None:
                continue  # 名單有、但沒行情也沒舊資料（多半是掛牌了還沒開始交易）
            if prev is None and is_dr(code, abbrev):
                continue  # 台灣存託憑證是外國公司掛牌，不算台股，原本的牌組也沒收
            if q is None:
                # 這輪沒抓到這檔的行情（停牌、或整個市場 feed 掛了）→ 沿用舊值
                v, p = prev.get("v", 0), prev.get("p", "---")
                stale += 1
            else:
                v, p = q
                v = 0 if v is None else v
                # 當日無成交 → 量記 0，價沿用上次有成交的價
                p = p or (prev.get("p") if prev else None) or "---"

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

    live = {s["c"] for s in stocks}
    dropped = [c for c in old if c not in live]

    # 安全閥：這是無人看管的每日排程，名單 API 若回傳殘缺資料，
    # 不該讓牌組被砍掉一大塊。下降超過 5% 就中止，寧可今天不更新。
    if len(stocks) < len(old) * 0.95:
        raise SystemExit(
            f"中止：檔數從 {len(old)} 掉到 {len(stocks)}（少了 {len(old) - len(stocks)} 檔，>5%），"
            "多半是上市櫃名單 API 回傳不完整，今天不更新。")

    stocks.sort(key=lambda s: (s["c"]))
    payload = {"date_twse": tw_date or payload.get("date_twse"),
               "date_tpex": tp_date or payload.get("date_tpex"),
               "stocks": stocks}

    new_line = "const PAYLOAD = " + json.dumps(payload, ensure_ascii=False) + ";"
    out = src[:m.start()] + new_line + src[m.end():]

    changed = out != src
    print(f"\n共 {len(stocks)} 檔｜新增 {len(added)}｜移除 {len(dropped)}｜沿用舊值 {stale}")
    if added:
        print("  新增:", ", ".join(added[:20]))
    if dropped:
        print("  移除:", ", ".join(dropped[:20]))

    if not changed:
        print("內容沒變，不寫檔（workflow 會靠 git diff 自己跳過 commit）。")
        return 0

    open(INDEX, "w", encoding="utf-8").write(out)
    save_history(stocks, tw_date, tp_date)
    print("已更新 index.html")
    return 0


def save_history(stocks, tw_date, tp_date):
    """每個交易日存一個小檔（~20KB），之後要做 20 日均量篩選就有料。

    刻意「一天一檔」而不是集中在單一檔案：這個 repo 每天都會 commit，
    單一大檔每天整份重寫會讓 git 每天多存一顆 1MB 的 blob，一年就腫成幾百 MB。
    """
    os.makedirs(HIST_DIR, exist_ok=True)
    key = tw_date or tp_date
    path = os.path.join(HIST_DIR, f"{key}.json")
    json.dump({s["c"]: s["v"] for s in stocks},
              open(path, "w", encoding="utf-8"), separators=(",", ":"))
    files = sorted(f for f in os.listdir(HIST_DIR) if f.endswith(".json"))
    for f in files[:-HIST_KEEP]:
        os.remove(os.path.join(HIST_DIR, f))
    print(f"成交量歷史：{min(len(files), HIST_KEEP)} 個交易日（{HIST_DIR}）")


if __name__ == "__main__":
    sys.exit(main())
