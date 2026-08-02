#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${MAPS_MONITOR_DIR:-/opt/maps-monitor}"
VERSION="${1:-}"

if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "用法：sudo ./deploy/update.sh v0.5.0" >&2
  exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "請使用 sudo 執行更新程式" >&2
  exit 1
fi
cd "$PROJECT_DIR"
if [[ ! -d .git ]]; then
  echo "$PROJECT_DIR 不是 Git 儲存庫" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "偵測到已追蹤檔案有本機修改，停止更新" >&2
  exit 1
fi

previous="$(git rev-parse HEAD)"
backup_path="$(docker compose run --rm monitor backup)"
echo "部署前備份：$backup_path"

# Invoked indirectly by the ERR trap below.
# shellcheck disable=SC2317
rollback() {
  trap - ERR
  echo "部署失敗，回復前一版本 $previous" >&2
  git checkout --detach "$previous"
  docker compose build
  ./deploy/install.sh
  docker compose up -d web
}
trap rollback ERR

git fetch --tags --force origin
git rev-parse --verify "refs/tags/$VERSION" >/dev/null
git checkout --detach "$VERSION"
docker compose build
./deploy/install.sh
docker compose run --rm monitor build-thumbnails
docker compose up -d web

for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:8000/healthz >/dev/null; then
    trap - ERR
    echo "部署完成：$VERSION"
    exit 0
  fi
  sleep 2
done
echo "Web 健康檢查逾時" >&2
exit 1
