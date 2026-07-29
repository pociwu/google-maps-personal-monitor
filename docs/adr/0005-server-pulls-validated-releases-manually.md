# Ubuntu 手動部署 GitHub 驗證版本

GitHub Actions 僅執行測試與敏感資料掃描，不持有 Ubuntu 的 SSH 私鑰或部署權限。操作者在 Ubuntu 執行 `deploy/update.sh`，由主機備份資料庫、取得驗證版本、重建容器並進行健康檢查；失敗時保留目前運行版本。
