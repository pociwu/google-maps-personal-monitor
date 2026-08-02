# Google Maps 個人評論監控

部署於 Ubuntu 24.04 ARM64 的唯讀監控工具，最多監控 10 個 Google Maps 公開貢獻者評論頁面。系統保存評論與圖片、辨識新增／修改／刪除／恢復、推算發表日期，並以 Telegram 逐則通知。

> A read-only Google Maps public contributor review monitor with Telegram alerts,
> date evidence, permanent image archives, and a Tailscale-friendly dashboard.
> This independent project is not affiliated with Google.

## 核心特性

- 未登入、無 Cookie；只允許展開評論全文，不會按讚、愛心、回覆或分享。
- 正常巡查每 6～8 小時；日期切換附近可每 30～60 分鐘密集觀察。
- 初次匯入靜默保存並傳送摘要；其後 24 小時觀察期不傳送內容事件。
- Telegram 逐則發送，間隔 6～10 秒，已嘗試事件不重複發送。
- 永久保存評論文字、星等與店家資料的每次版本；修改通知附前後摘要。
- 原圖永久保存；同一評論以原始檔、精確像素及嚴格有損重編碼比對去除重複圖片引用，另產生 480px WebP 縮圖。
- SQLite 保存日期證據、評論狀態與事件；每日建立本機快照。
- 提供 Tailscale 使用的單頁唯讀 Web 儀表板。

## 唯讀互動保證

抓取器只會開啟公開網址、讀取 DOM 與圖片、直接設定評論容器的捲動位置，以及啟動評論文字同一容器內、標籤精確符合「更多／顯示更多／閱讀更多／More／Show more／Read more」的全文展開控制。按讚、有幫助、愛心、回覆、回應、分享等標籤設有拒絕清單；抓取器不傳送滑鼠、觸控、鍵盤或表單輸入，也不登入或載入 Cookie。

若任何評論仍呈現摘要，該對象整輪巡查失敗並保存除錯 HTML／截圖，不會以截斷內容覆蓋資料。`tests/test_read_only.py` 會阻止全文展開白名單以外的互動 API 進入抓取器。

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

## Web 儀表板

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
- 可拖曳貢獻者卡片調整順序，順序永久保存於監控設定；「全部貢獻者」固定在最前面。
- 每張卡片提供公開 Google Maps 個人頁面連結，可在新分頁拜訪。
- 可在首頁輸入公開貢獻者評論網址；通過格式與連線驗證後，以唯讀瀏覽器辨識公開頁面的貢獻者名稱，再加入下一輪巡查。
- 每張貢獻者卡片可停止監控；只移出設定清單，不刪除評論、證據或圖片。
- 新增、移除與排序不需要密碼；能開啟首頁的人都可以操作。
- 支援店家／內容搜尋、狀態與星等篩選。
- 每頁顯示 20 筆，日期由新到舊，未知日期排最後。
- 預設隱藏已刪除評論。
- 僅評分項目顯示「（沒有文字）」與「僅評分」。
- 已確認日期直接顯示，估算日期加「約」。
- 星等旁顯示 Google 最近一次提供的相對時間。
- 每則評論可直接開啟響應式日期推算證據頁，不依賴 JavaScript，所有時間統一為 Asia/Taipei。
- 有兩個以上版本的評論可開啟修改差異頁，紅色標示刪除、綠色標示新增。
- 優先連到 Google 單則評論；無法取得時明確降級為店家連結，舊資料再降級為 Google Maps 店家搜尋。
- 縮圖延遲載入，燈箱才讀取原圖。
- 目前圖片直接顯示；連續兩輪確認移除的永久原圖收進「歷史圖片」。
- Web 使用原子產生的 SQLite 展示快照；快照、原圖與縮圖皆以唯讀方式掛載。
- 唯一寫入範圍是 `config/targets.yaml`；沒有評論資料寫入或公開 JSON API。
- 一般 access log 關閉，不記錄搜尋字串。
- 所有回應加入 `noindex`、CSP 與其他安全標頭。
- 使用固定版本 Bootstrap CDN，不含追蹤分析。

新增與移除沒有身分驗證；若不希望其他人變更監控清單，請只在可信網路提供 TCP 8000。

健康檢查：

```bash
curl http://127.0.0.1:8000/healthz
```

## 日期證據

日期引擎永久保存每次相對時間原文、解析單位、可能區間、可信度、時間主體及模型版本。公開 DOM 若提供完整時間戳，必須連續兩輪一致才採用。天與週需要相鄰數字切換並二次確認；月份與年份各需至少 3 個一致樣本完成模型校準。

舊資料庫由新版第一次開啟前會備份至：

```text
state/backups/pre-schema-v7-*.sqlite3
```

遷移失敗會自動還原。舊推算日期保留為 `legacy_publish_date`。

schema v7 會先備份，再把升級當下的每則評論建立為第 1 版。之後文字、星等或
店家資料改變時永久新增版本，不覆蓋歷史內容。升級前已經被覆蓋的舊文字無法
回復，因此差異紀錄從 v0.4.0 部署後開始累積。

schema v6 會先備份，再為既有圖片建立精確像素與嚴格感知指紋。同一評論內
原始檔 SHA-256 相同、解碼後像素完全相同，或同尺寸 JPEG／WebP／AVIF 通過
差分雜湊及 64px RGB 誤差雙重門檻時，只保留最早成功保存的一筆。PNG 等無損
格式不套用感知比對。舊圖片後續只有在連續兩次完整巡查都缺失時才移入歷史
圖片。Google 圖片網址或有損重新編碼版本的變動不再誤判為評論修改。

## 資料位置

```text
監控 SQLite  state/data/monitor.sqlite3
Web 快照     state/web/monitor.sqlite3
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
sudo git clone --branch v0.5.0 --depth 1 \
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
sudo ./deploy/update.sh v0.5.0
```

更新程式會先備份、取得指定標籤、重建映像、補建縮圖並執行 Web 健康檢查；失敗時回到部署前的程式版本。

Web 不直接開啟 WAL 模式的監控資料庫。每次監控或資料操作結束時，程式會透過
SQLite Backup API 原子更新 `state/web/monitor.sqlite3`；巡查期間網站持續顯示上一份
完整快照。需要手動重建時可執行：

```bash
sudo docker compose run --rm monitor refresh-dashboard
sudo docker compose up -d --force-recreate web
```

## 開發驗證

```bash
python -m pip install ".[test]"
python -m compileall -q src tests
ruff check src tests
pytest
shellcheck deploy/*.sh
```

專案使用 [MIT License](LICENSE)。共同術語位於 [CONTEXT.md](CONTEXT.md)，架構決策位於 `docs/adr/`。
