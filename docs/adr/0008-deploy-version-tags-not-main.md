# Ubuntu 只部署版本標籤

`main` 用於整合及驗證，Ubuntu 不直接追蹤其最新提交。穩定版本建立語意化版本標籤與 GitHub Release，主機以 `deploy/update.sh <version>` 部署指定標籤並保存前一版本；首個包含唯讀儀表板的版本為 `v0.2.0`。
