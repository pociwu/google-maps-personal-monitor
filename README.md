# Google Maps 個人評論監控

部署於 Ubuntu 24.04 ARM64 的唯讀監控工具，最多監控 10 個 Google Maps 公開貢獻者評論頁面。系統保存評論與圖片、辨識新增／修改／刪除／恢復、推算發表日期，並以 Telegram 逐則通知。

> A read-only Google Maps public contributor review monitor with Telegram alerts,
> date evidence, permanent image archives, and a Tailscale-friendly dashboard.
> This independent project is not affiliated with Google.

## 核心特性

- 未登入、無 Cookie、零點擊抓取；不會按讚、愛心、回覆、分享或展開內容。
- 正常巡查每 6～8 小時；日期切換附近可每 30～60 分鐘密集觀察。
- 初次匯入靜默保存並傳送摘要；其後 24 小時觀察期不傳送內容事件。
- Telegram 逐則發送，間隔 6～10 秒，已嘗試事件不重複發送。
- 原圖永久保存，以 SHA-256 去重；另產生 480px WebP 縮圖。
- SQLite 保存日期證據、評論狀態與事件；每日建立本機快照。
- 提供 Tailscale 使用的單頁唯讀 Web 儀表板。

## 零點擊保證

抓取器只會開啟公開網址、讀取 DOM 與圖片、直接設定評論容器的捲動位置。它不傳送滑鼠、觸控、鍵盤、表單或 DOM 互動事件。完整資料無法在零點擊條件下取得時，該輪會失敗並保存除錯 HTML／截圖，不會改用點擊方式。

`tests/test_read_only.py` 會阻止互動 API 進入抓取器。

## 安裝

主機需要 Docker Engine 與 Compose 外掛。專案位置固定為 `/opt/maps-monitor`：

```bash
sudo mkdir -p /opt/maps-monitor
sudo cp -a . /opt/maps-monitor/
cd /opt/maps-monitor
sudo cp config/targets.example.yaml config/targets.yaml
sudo cp .env.example .env
```

編輯 `config/targets.yaml`：

```yaml
timezone: Asia/Taipei
locale: zh-TW
targets:
  - name: 使用者甲
    url: "https://www.google.com/maps/contrib/123456789012345678901/reviews"
    enabled: true
```

名稱與網址均不可重複，最多 10 位。停用對象只需改為 `enabled: false`，歷史資料不會刪除。

編輯 `.env`：

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_CHAT_ID=replace_me
DASHBOARD_STALE_WARNING_HOURS=12
DASHBOARD_STALE_CRITICAL_HOURS=24
```

建立映像並測試：

```bash
sudo chmod 600 .env
sudo chmod +x deploy/install.sh deploy/update.sh deploy/maps-monitor-menu.sh
sudo docker compose build --pull
sudo docker compose run --rm monitor test-telegram
sudo docker compose run --rm monitor run-and-send
sudo docker compose run --rm monitor build-thumbnails
sudo ./deploy/install.sh
```

安裝程式會設定：

- `maps-monitor.timer`：每 6～8 小時正常巡查。
- `maps-monitor-dense.timer`：每 30～60 分鐘檢查到期的日期密集觀察。
- `maps-monitor-backup.timer`：每日備份。
- `maps-monitor-web.service`：Web 容器常駐及開機啟動。
- `~/maps-monitor-menu.sh`：主機查詢選單。

安裝程式不會建立或修改 UFW、iptables、OCI Security List 或其他防火牆規則。

## 唯讀 Web 儀表板

Web 容器發布：

```text
0.0.0.0:8000
```

第一階段建議只透過 Tailscale 瀏覽：

```text
http://100.x.x.x:8000/
```

請自行確保一般公網沒有開放 TCP 8000。儀表板：

- 以可選取卡片顯示所有貢獻者。
- 支援店家／內容搜尋、狀態與星等篩選。
- 每頁顯示 20 筆，日期由新到舊，未知日期排最後。
- 預設隱藏已刪除評論。
- 僅評分項目顯示「（沒有文字）」與「僅評分」。
- 已確認日期直接顯示，估算日期加「約」。
- 縮圖延遲載入，燈箱才讀取原圖。
- SQLite、原圖與縮圖皆以唯讀方式掛載。
- 沒有資料寫入路由、管理介面、下載功能或公開 JSON API。
- 一般 access log 關閉，不記錄搜尋字串。
- 所有回應加入 `noindex`、CSP 與其他安全標頭。
- 使用固定版本 Bootstrap CDN，不含追蹤分析。

健康檢查：

```bash
curl http://127.0.0.1:8000/healthz
```

## 日期證據

日期引擎永久保存每次相對時間原文、解析單位、可能區間、可信度、時間主體及模型版本。公開 DOM 若提供完整時間戳，必須連續兩輪一致才採用。天與週需要相鄰數字切換並二次確認；月份與年份各需至少 3 個一致樣本完成模型校準。

舊資料庫由新版第一次開啟前會備份至：

```text
state/backups/pre-schema-v3-*.sqlite3
```

遷移失敗會自動還原。舊推算日期保留為 `legacy_publish_date`。

## 資料位置

```text
SQLite       state/data/monitor.sqlite3
原圖         state/data/images/<前兩碼>/<SHA-256>.<格式>
縮圖         state/data/images/thumbnails/<前兩碼>/<SHA-256>.webp
除錯資料     state/debug/
每日快照     state/backups/
```

補建既有縮圖：

```bash
sudo docker compose run --rm monitor build-thumbnails
```

手動備份：

```bash
sudo docker compose run --rm monitor backup
```

CSV／JSON 匯出只在主機命令列提供：

```bash
sudo docker compose run --rm monitor export \
  --format csv --output /app/state/export/reviews.csv
```

## 日常操作

```bash
systemctl list-timers 'maps-monitor*' --all --no-pager --full
sudo systemctl start maps-monitor.service
sudo docker compose run --rm monitor status
journalctl -u maps-monitor.service -n 100 --no-pager
journalctl -u maps-monitor-web.service -n 100 --no-pager
~/maps-monitor-menu.sh
```

## GitHub 與版本部署

公開儲存庫：

```text
https://github.com/pociwu/google-maps-personal-monitor
```

公開 Git 歷史永遠排除 `.env`、真實 `config/targets.yaml`、`state/`、SQLite、圖片、備份、Telegram Token 與真實貢獻者網址。GitHub Actions 執行 Python 測試、Ruff、ShellCheck、Gitleaks 與 ARM64 Docker build。

### 將既有手動安裝轉為 Git 管理

如果 `/opt/maps-monitor` 是先前以 ZIP 或手動複製安裝、目錄內沒有 `.git`，首次升級請執行：

```bash
sudo systemctl stop maps-monitor.timer maps-monitor-web.service
cd /opt
stamp="$(date +%Y%m%d-%H%M%S)"
sudo mv maps-monitor "maps-monitor.pre-git-${stamp}"
sudo git clone --branch v0.2.0 --depth 1 \
  https://github.com/pociwu/google-maps-personal-monitor.git maps-monitor
sudo cp "maps-monitor.pre-git-${stamp}/.env" maps-monitor/.env
sudo cp "maps-monitor.pre-git-${stamp}/config/targets.yaml" \
  maps-monitor/config/targets.yaml
sudo mv "maps-monitor.pre-git-${stamp}/state" maps-monitor/state
cd maps-monitor
sudo docker compose build
sudo docker compose run --rm monitor build-thumbnails
sudo ./deploy/install.sh
curl --fail http://127.0.0.1:8000/healthz
```

確認評論、圖片、Telegram 與網頁都正常後，才自行移除
`/opt/maps-monitor.pre-git-*`；它是首次轉換時保留的完整回復副本。

Ubuntu 只部署版本標籤，不直接跟隨 `main`：

```bash
cd /opt/maps-monitor
sudo ./deploy/update.sh v0.2.0
```

更新程式會先備份、取得指定標籤、重建映像、補建縮圖並執行 Web 健康檢查；失敗時回到部署前的程式版本。

## 開發驗證

```bash
python -m pip install ".[test]"
python -m compileall -q src tests
ruff check src tests
pytest
shellcheck deploy/*.sh
```

專案使用 [MIT License](LICENSE)。共同術語位於 [CONTEXT.md](CONTEXT.md)，架構決策位於 `docs/adr/`。
