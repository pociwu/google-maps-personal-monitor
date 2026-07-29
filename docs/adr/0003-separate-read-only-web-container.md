---
status: superseded by ADR-0006
---

# 使用獨立的唯讀 Web 容器

公開儀表板由獨立的 FastAPI 與 Jinja Web 容器提供，透過唯讀掛載即時讀取 SQLite 和永久圖片，再由 Nginx 對外服務。Web 容器不共用監控命令入口，也不提供資料寫入或管理 API，使公開網站遭到請求時不能改變監控狀態。
