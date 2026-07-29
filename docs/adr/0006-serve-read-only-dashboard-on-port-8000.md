# 以 port 8000 提供 Tailscale 唯讀儀表板

唯讀儀表板由獨立的 FastAPI 與 Jinja Web 容器提供，透過唯讀掛載即時讀取 SQLite 和永久圖片，並以 `0.0.0.0:8000` 發布 HTTP。第一階段由使用者透過 Tailscale 位址存取，不安裝 Nginx、不使用網域或 TLS，部署程式也不建立或修改任何主機或雲端防火牆規則。
