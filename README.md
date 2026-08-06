# 台股翻牌練習

單檔 HTML 的台股代號／名稱記憶卡。`index.html` 打開就能用，沒有相依套件。

## 每日自動更新

`.github/workflows/update-quotes.yml` 會在**台北時間週一～週五 14:30**（另有 15:30 補跑一次）
更新 `index.html` 裡每檔的成交量與收盤價，有變動才 commit。

> **為什麼不是 13:30**：台股 13:30 收盤，但證交所／櫃買的收盤行情大約 14:00–14:30 才發布。
> 13:30 去抓一定只拿得到前一個交易日的數字，所以排在 14:30。

資料來源都是官方公開資料，不需要任何 API key：

| 用途 | 來源 |
|---|---|
| 上市行情 | `twse.com.tw/rwd/zh/afterTrading/MI_INDEX` (`type=ALLBUT0999`) |
| 上櫃行情 | `tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` |
| 上市名單／產業別 | `openapi.twse.com.tw/v1/opendata/t187ap03_L` |
| 上櫃名單／產業別 | `tpex.org.tw/openapi/v1/mopsfin_t187ap03_O` |

腳本只覆寫每檔的 `v`（成交張數）與 `p`（收盤價），
公司名 `n`／產業 `i`／市場 `m`／業務描述 `d` 一律沿用舊值，不會被洗掉。

### 啟用前要做的事

GitHub → repo **Settings → Actions → General → Workflow permissions**
選 **Read and write permissions**，否則 workflow 沒有權限 push。

要立刻試跑：Actions 分頁 → 「每日更新成交量與收盤價」→ Run workflow。

### 本機手動跑

```bash
python3 scripts/update_quotes.py
```

## 檔案

```
index.html                     整個 app（含 PAYLOAD 股票資料）
scripts/update_quotes.py       每日行情更新
data/volume/YYYMMDD.json       每個交易日的成交量（保留 60 天，供日後做均量篩選）
data/descriptions.json         業務描述覆寫檔（選用，{"2330": "..."}）
.github/workflows/             排程
```

### `data/descriptions.json`

想補／改某檔的一句話業務描述，寫在這裡就好，不用去動 `index.html`：

```json
{
  "2330": "晶圓代工龍頭，先進製程市占逾七成",
  "2317": "全球最大電子代工廠，AI 伺服器組裝為主要成長動能"
}
```

更新腳本每次跑都會把這個檔合併進 `index.html`，優先權高於 `index.html` 裡既有的 `d`。

## 資料規則

- 收錄範圍：上市 + 上櫃的**普通股**。ETF、權證、台灣存託憑證（`-DR`）都不收。
- 當日無成交：量記 `0`，價沿用上次有成交的收盤價。
- 新上市櫃公司會自動加入（名稱取官方簡稱，產業別由代碼對照補上）。
- 安全閥：檔數若比前一版少超過 5%，判定為來源資料殘缺，當天中止不更新。
