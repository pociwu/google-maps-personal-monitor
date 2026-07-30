---
status: accepted
---

# 為 Web 發布原子 SQLite 展示快照

監控資料庫使用 WAL 模式。Web 容器若透過唯讀掛載直接開啟該資料庫，在巡查程序關閉並移除 `-wal`、`-shm` 後，SQLite 可能需要在資料庫目錄建立輔助檔案，因掛載唯讀而回報 `unable to open database file`。

監控與其他會開啟資料庫的 CLI 操作結束時，使用 SQLite Backup API 將一致狀態寫入暫存檔，切換為 DELETE journal mode，再以原子重新命名發布至 `state/web/monitor.sqlite3`。Web 只掛載並讀取此展示快照；永久原圖與縮圖仍以獨立唯讀掛載提供。

巡查進行期間，Web 持續顯示上一份完整快照；操作完成後才整體切換，不會看見部分寫入。代價是額外保存一份小型 SQLite 檔案，且頁面資料最多落後一輪尚未完成的巡查。
