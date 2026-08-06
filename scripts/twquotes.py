"""證交所／櫃買中心公開行情的共用抓取層。

所有端點都是官方公開資料，不需要 API key。
update_quotes.py（每日更新）與 backfill_volume.py（回補歷史）共用這裡。
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import timedelta

HEADERS = {
    # 櫃買中心擋在 Cloudflare 後面，從 GitHub Actions 的機房 IP 過去很容易吃到 520
    # （origin 回了空回應）。帶齊瀏覽器會送的標頭可以明顯降低機率。
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}
_LAST_CALL = [0.0]
MIN_GAP = 0.7  # 對官方站台客氣一點，兩次請求至少間隔這麼久

# 520/521/522/523/524 都是 Cloudflare 在講「我連不到後面的 origin」——
# 這是對面暫時性的毛病，值得等久一點再試。
TRANSIENT = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def _ssl_ctx():
    """macOS 內建 Python 常缺 root CA；有 certifi 就用，沒有就用系統預設。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


CTX = _ssl_ctx()


def fetch(url, tries=5, referer=None):
    """抓 JSON，遇到暫時性錯誤指數退避重試。全部失敗才丟 RuntimeError。"""
    last = None
    for i in range(tries):
        gap = MIN_GAP - (time.monotonic() - _LAST_CALL[0])
        if gap > 0:
            time.sleep(gap)
        _LAST_CALL[0] = time.monotonic()
        try:
            h = dict(HEADERS)
            if referer:
                h["Referer"] = referer
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 網路層什麼都可能炸
            last = e
            code = getattr(e, "code", None)
            print(f"  取數失敗({i + 1}/{tries}) {url} :: {e}", file=sys.stderr)
            if i == tries - 1:
                break
            if code is not None and code not in TRANSIENT:
                break  # 404 之類重試也沒用，直接放棄
            time.sleep(min(2 ** i * 2, 30))  # 2, 4, 8, 16 秒
    raise RuntimeError(f"取數連續失敗: {url} :: {last}")


def fetch_or_none(url, **kw):
    """抓不到就回 None，讓呼叫端自己決定要不要降級，而不是整支腳本掛掉。"""
    try:
        return fetch(url, **kw)
    except RuntimeError as e:
        print(f"  ⚠ {e}", file=sys.stderr)
        return None


# ---------- 數值處理 ----------

def num(s):
    """'20,385,198' -> 20385198.0；'--' / '' -> None"""
    if s is None:
        return None
    s = re.sub(r"<[^>]*>", "", str(s)).replace(",", "").strip()
    if not s or s in {"--", "---", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def price(s):
    """收盤價保留兩位小數字串（'23.80'），無成交回 None。"""
    v = num(s)
    return None if v is None else f"{v:.2f}"


def lots(shares):
    """股 -> 張"""
    v = num(shares)
    return None if v is None else int(round(v / 1000))


def to_roc(yyyymmdd):
    """'20260806' -> '1150806'"""
    s = str(yyyymmdd)
    return f"{int(s[:4]) - 1911:03d}{s[4:]}"


# ---------- 行情 ----------

def twse_quotes(date):
    """上市每日收盤行情。date 為 datetime。回傳 (民國日期字串, {代號: (張數, 收盤價)})。

    非交易日回 (None, {})。
    """
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?date={date.strftime('%Y%m%d')}&type=ALLBUT0999&response=json")
    d = fetch_or_none(url, referer="https://www.twse.com.tw/")
    if not d or d.get("stat") != "OK":
        return None, {}
    table = next((t for t in d.get("tables", [])
                  if "每日收盤行情" in t.get("title", "")), None)
    if not table:
        return None, {}
    f = table["fields"]
    ic, iv, ip = f.index("證券代號"), f.index("成交股數"), f.index("收盤價")
    out = {row[ic].strip(): (lots(row[iv]), price(row[ip])) for row in table["data"]}
    return to_roc(d.get("date") or date.strftime("%Y%m%d")), out


def tpex_quotes(date):
    """上櫃每日收盤行情（不含定價）。同 twse_quotes 的回傳格式。"""
    url = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
           f"?date={date.strftime('%Y/%m/%d')}&type=EW&id=&response=json")
    d = fetch_or_none(url, referer="https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html")
    if not d or str(d.get("stat", "")).lower() != "ok":
        return None, {}
    tables = d.get("tables") or []
    table = next((t for t in tables if t.get("data")), None)
    if not table:
        return None, {}
    f = [str(x).strip() for x in table["fields"]]

    def idx(*names):
        for n in names:
            for i, col in enumerate(f):
                if col.replace(" ", "").startswith(n):
                    return i
        raise KeyError(names)

    ic, iv, ip = idx("代號"), idx("成交股數"), idx("收盤")
    out = {str(row[ic]).strip(): (lots(row[iv]), price(row[ip])) for row in table["data"]}
    return to_roc(d.get("date") or date.strftime("%Y%m%d")), out


def quotes_for(date):
    """同時抓兩市，回傳 (民國日, {代號: (張, 價)}, 民國日, {...})。"""
    tw_d, tw_q = twse_quotes(date)
    tp_d, tp_q = tpex_quotes(date)
    return tw_d, tw_q, tp_d, tp_q


def recent_trading_days(end, back=14):
    """從 end 往回一天一天吐 datetime，交給呼叫端自己判斷是不是交易日。"""
    for i in range(back):
        yield end - timedelta(days=i)


# ---------- 上市櫃名單 ----------

def universe_cache_path(root):
    return os.path.join(root, "data", "universe.json")


def universes(root):
    """回傳 ({上市代號: (產業代碼, 官方簡稱)}, {上櫃...})。

    用途是界定「哪些是真的上市／上櫃公司」——行情表裡混了 ETF、權證，靠這個濾掉。

    這份名單一個月才動個幾筆，但櫃買那支 API 從 GitHub Actions 打過去常吃 Cloudflare 520。
    為了不讓「名單暫時抓不到」害整天的行情更新泡湯，成功時寫入 data/universe.json，
    失敗時回退到快取——名單舊個一兩天完全無所謂，行情才是每天要更新的東西。
    """
    cache_path = universe_cache_path(root)
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    def resolve(key, url, parse, referer=None):
        raw = fetch_or_none(url, referer=referer)
        if raw:
            got = parse(raw)
            if got:
                return got, True
        old = cache.get(key)
        if old:
            print(f"  ⚠ {key} 名單抓不到，改用快取（{len(old)} 家，存於 {cache.get('_date', '?')}）",
                  file=sys.stderr)
            return {k: tuple(v) for k, v in old.items()}, False
        raise RuntimeError(f"{key} 名單抓不到，也沒有快取可用，中止")

    listed, tw_fresh = resolve(
        "listed", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        lambda rows: {str(r["公司代號"]).strip():
                      (str(r.get("產業別", "")).strip(), str(r.get("公司簡稱", "")).strip())
                      for r in rows if len(str(r["公司代號"]).strip()) == 4},
        referer="https://openapi.twse.com.tw/")
    otc, tp_fresh = resolve(
        "otc", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
        lambda rows: {str(r["SecuritiesCompanyCode"]).strip():
                      (str(r.get("SecuritiesIndustryCode", "")).strip(),
                       str(r.get("CompanyAbbreviation", "")).strip())
                      for r in rows if len(str(r["SecuritiesCompanyCode"]).strip()) == 4},
        referer="https://www.tpex.org.tw/")

    out = dict(cache)
    if tw_fresh:
        out["listed"] = {k: list(v) for k, v in listed.items()}
    if tp_fresh:
        out["otc"] = {k: list(v) for k, v in otc.items()}
    # 名單一個月才動幾筆，但這個檔有 48KB。只有內容真的變了才重寫，
    # 否則每個交易日都會多一顆 48KB 的 blob，一年白白胖十幾 MB。
    if {k: v for k, v in out.items() if k != "_date"} != \
       {k: v for k, v in cache.items() if k != "_date"}:
        out["_date"] = time.strftime("%Y-%m-%d")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        json.dump(out, open(cache_path, "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        print(f"  名單有異動，已更新快取（{cache_path}）")
    return listed, otc


def is_dr(code, name):
    """台灣存託憑證（91xx／名稱帶 -DR）：外國公司來台掛牌，不收進台股牌組。"""
    return name.endswith("-DR") or (code.startswith("91") and not name)


def industry_map(uni, stocks_by_code, market):
    """用現有 payload 的產業中文名反推「產業代碼 -> 中文名」，新上市櫃公司才有產業可填。"""
    tally = defaultdict(Counter)
    for code, (ind, _) in uni.items():
        s = stocks_by_code.get(code)
        if s and s.get("m") == market and s.get("i"):
            tally[ind][s["i"]] += 1
    return {k: v.most_common(1)[0][0] for k, v in tally.items()}


# ---------- payload 讀寫 ----------

PAYLOAD_RE = re.compile(r"const PAYLOAD = (\{.*?\});", re.S)


def read_payload(index_path):
    src = open(index_path, encoding="utf-8").read()
    m = PAYLOAD_RE.search(src)
    if not m:
        raise SystemExit("index.html 找不到 const PAYLOAD = {...};")
    return src, m, json.loads(m.group(1))


def write_payload(index_path, src, m, payload):
    """回傳是否真的有變動。"""
    line = "const PAYLOAD = " + json.dumps(payload, ensure_ascii=False) + ";"
    out = src[:m.start()] + line + src[m.end():]
    if out == src:
        return False
    open(index_path, "w", encoding="utf-8").write(out)
    return True


# ---------- 成交量歷史 ----------

def hist_dir(root):
    return os.path.join(root, "data", "volume")


def save_day(root, roc_date, code_to_lots, keep=60):
    """每個交易日存一個小檔（~20KB）。

    刻意「一天一檔」而不是集中在單一大檔：這個 repo 每天都會 commit，
    單一大檔每天整份重寫會讓 git 每天多存一顆 1MB 的 blob，一年腫成幾百 MB。
    """
    d = hist_dir(root)
    os.makedirs(d, exist_ok=True)
    json.dump(code_to_lots, open(os.path.join(d, f"{roc_date}.json"), "w", encoding="utf-8"),
              separators=(",", ":"))
    files = sorted(f for f in os.listdir(d) if re.fullmatch(r"\d{7}\.json", f))
    for f in files[:-keep]:
        os.remove(os.path.join(d, f))


def load_history(root, days=20):
    """讀最近 N 個交易日的量，回傳 ({代號: 均量}, 實際用到的天數)。

    均量只除以「該檔有出現的天數」，新上市或停牌過的股票才不會被稀釋。
    """
    d = hist_dir(root)
    if not os.path.isdir(d):
        return {}, 0
    files = sorted(f for f in os.listdir(d) if re.fullmatch(r"\d{7}\.json", f))[-days:]
    if not files:
        return {}, 0
    total, seen = defaultdict(int), defaultdict(int)
    for f in files:
        for code, v in json.load(open(os.path.join(d, f), encoding="utf-8")).items():
            total[code] += v
            seen[code] += 1
    return {c: int(round(total[c] / seen[c])) for c in total}, len(files)
