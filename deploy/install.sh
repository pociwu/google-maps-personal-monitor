#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "請使用 sudo 執行此安裝程式" >&2
  exit 1
fi

deploy_user="${SUDO_USER:-root}"
deploy_group="$(id -gn "$deploy_user")"
deploy_home="$(getent passwd "$deploy_user" | cut -d: -f6)"
if [[ -z "$deploy_home" || ! -d "$deploy_home" ]]; then
  echo "找不到使用者 $deploy_user 的家目錄" >&2
  exit 1
fi
install -d -m 2750 -o root -g "$deploy_group" /opt/maps-monitor/state
chgrp -R "$deploy_group" /opt/maps-monitor/state
find /opt/maps-monitor/state -type d -exec chmod 2750 {} +
find /opt/maps-monitor/state -type f -exec chmod 0640 {} +
install -m 0644 deploy/systemd/maps-monitor.service /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor.timer /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor-failure.service /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor-dense.service /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor-dense.timer /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor-backup.service /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor-backup.timer /etc/systemd/system/
install -m 0644 deploy/systemd/maps-monitor-web.service /etc/systemd/system/
install -m 0755 -o "$deploy_user" -g "$deploy_group" \
  deploy/maps-monitor-menu.sh "$deploy_home/maps-monitor-menu.sh"
chmod 0600 /opt/maps-monitor/.env
systemctl daemon-reload
systemctl enable --now maps-monitor.timer maps-monitor-dense.timer maps-monitor-backup.timer
systemctl enable --now maps-monitor-web.service
systemctl list-timers 'maps-monitor*'
