---
status: superseded by ADR-0006
---

# 公開儀表板部署於監控主機

公開儀表板部署在現有 Ubuntu 監控主機，以直接使用 SQLite 與永久圖片；GitHub 僅管理程式碼、部署設定及文件。這避免巡查後反覆把監控資料推送至 GitHub，也確保 `.env`、Telegram 密鑰、資料庫和圖片不會進入原始碼儲存庫。
