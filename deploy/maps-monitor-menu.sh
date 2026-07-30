#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${MAPS_MONITOR_DIR:-/opt/maps-monitor}"

if [[ ! -f "$PROJECT_DIR/compose.yaml" ]]; then
  echo "找不到專案：$PROJECT_DIR"
  echo "如安裝在其他位置，請設定 MAPS_MONITOR_DIR。"
  exit 1
fi

show_reviews() {
  local output
  output="$(mktemp)"
  cd "$PROJECT_DIR"

  # The redirect intentionally runs as the calling user; mktemp created this file.
  # shellcheck disable=SC2024
  if ! sudo docker compose run --rm --entrypoint python monitor - >"$output" <<'PY'
import sqlite3
import textwrap
from pathlib import Path

database = Path("/app/state/data/monitor.sqlite3")
if not database.exists():
    raise SystemExit("尚未建立評論資料庫。")

connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    """SELECT t.name AS target_name,r.place_name,r.rating,r.publish_date,
              r.edit_date,r.status,r.body,r.last_seen_at
       FROM reviews r JOIN targets t ON t.id=r.target_id
       ORDER BY t.name,r.publish_date DESC,r.id DESC"""
).fetchall()

print(f"評論總數：{len(rows)}")
print("搜尋方式：進入畫面後按 /，輸入人物或店家名稱。按 q 離開。")
print("=" * 72)
for index, row in enumerate(rows, 1):
    rating = "-" if row["rating"] is None else f"{row['rating']:g}"
    print(f"[{index}] 人物：{row['target_name']}")
    print(f"店家：{row['place_name']}")
    print(f"星等：{rating}　狀態：{row['status']}")
    print(f"推算發表日期：{row['publish_date'] or '-'}")
    if row["edit_date"]:
        print(f"推算修改日期：{row['edit_date']}")
    print(f"最後發現時間：{row['last_seen_at']}")
    print("評論內容：")
    print(textwrap.indent(row["body"] or "（沒有文字）", "  "))
    print("-" * 72)
connection.close()
PY
  then
    echo "讀取評論失敗。"
    rm -f "$output"
    return
  fi

  if command -v less >/dev/null 2>&1; then
    less -R "$output"
  else
    cat "$output"
  fi
  rm -f "$output"
}

show_schedule() {
  echo
  echo "=== 前回執行結果 ==="
  sudo systemctl show maps-monitor.service \
    --property=Result \
    --property=ExecMainStatus \
    --property=ExecMainStartTimestamp \
    --property=ExecMainExitTimestamp \
    --no-pager

  echo
  echo "=== 最近執行紀錄 ==="
  sudo journalctl -u maps-monitor.service -n 15 --no-pager

  echo
  echo "=== 下次執行時間 ==="
  local timer_state last_trigger
  timer_state="$(sudo systemctl show maps-monitor.timer --property=ActiveState --value)"
  last_trigger="$(sudo systemctl show maps-monitor.timer --property=LastTriggerUSec --value)"
  echo "計時器狀態：${timer_state:-未知}"
  echo "上次觸發時間：${last_trigger:-尚未執行}"
  echo "完整排程（NEXT 為下次執行時間）："
  sudo systemctl list-timers maps-monitor.timer --all --no-pager --full
}

show_date_evidence() {
  local output
  output="$(mktemp)"
  cd "$PROJECT_DIR"

  # The redirect intentionally runs as the calling user; mktemp created this file.
  # shellcheck disable=SC2024
  if ! sudo docker compose run --rm --entrypoint python monitor - >"$output" <<'PY'
import sqlite3
import textwrap
from pathlib import Path
from maps_monitor.dates import normalize_relative_label, parse_relative

database = Path("/app/state/data/monitor.sqlite3")
if not database.exists():
    raise SystemExit("尚未建立評論資料庫。")
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
reviews = connection.execute(
    """SELECT r.id,t.name AS target_name,r.place_name,r.body,r.publish_date,r.publish_estimate,
              r.publish_earliest,r.publish_latest,r.precision,r.confidence,r.basis,
              r.time_subject,r.date_model_version,r.legacy_publish_date,r.edit_date,
              r.edit_estimate,r.edit_earliest,r.edit_latest,r.edit_confidence,r.edit_basis
       FROM reviews r JOIN targets t ON t.id=r.target_id
       ORDER BY t.name,r.id"""
).fetchall()
print(f"日期證據評論數：{len(reviews)}")
print("按 / 可搜尋人物、店家或評論 ID；按 q 離開。")
print("=" * 88)
for review in reviews:
    print(f"評論 ID：{review['id']}　人物：{review['target_name']}　店家：{review['place_name']}")
    print("評論內容：")
    print(textwrap.indent(review["body"] or "（沒有文字）", "  "))
    print(f"結果：{review['publish_estimate'] or '-'}　日期：{review['publish_date'] or '-'}")
    print(f"可信度：{review['confidence']}　精度：{review['precision']}")
    print(f"依據：{review['basis']}　時間主體：{review['time_subject']}")
    print(f"可能區間：{review['publish_earliest'] or '-'} ～ {review['publish_latest'] or '-'}")
    print(f"模型版本：{review['date_model_version'] or '-'}　舊日期：{review['legacy_publish_date'] or '-'}")
    if review["edit_estimate"]:
        print(f"最後編輯：{review['edit_estimate']}　日期：{review['edit_date'] or '-'}")
        print(f"編輯區間：{review['edit_earliest'] or '-'} ～ {review['edit_latest'] or '-'}")
        print(f"編輯可信度：{review['edit_confidence'] or '-'}　依據：{review['edit_basis'] or '-'}")
    observations = connection.execute(
        """SELECT observed_at,relative_time,parsed_count,parsed_unit,is_edit,exact_timestamp
           FROM observations WHERE review_id=? ORDER BY observed_at,id""",
        (review["id"],),
    ).fetchall()
    print("觀察紀錄：")
    for item in observations:
        label = normalize_relative_label(item["relative_time"])
        fallback = parse_relative(label)
        parsed_count = item["parsed_count"] if item["parsed_count"] is not None else (fallback.count if fallback else None)
        parsed_unit = item["parsed_unit"] or (fallback.unit if fallback else None)
        parsed = "-" if parsed_unit is None else f"{parsed_count} {parsed_unit}"
        print(
            f"  {item['observed_at']}｜{label}｜解析={parsed}｜"
            f"編輯={bool(item['is_edit'])}｜完整時間={item['exact_timestamp'] or '-'}"
        )
    print("-" * 88)
connection.close()
PY
  then
    echo "讀取日期證據失敗。"
    rm -f "$output"
    return
  fi
  if command -v less >/dev/null 2>&1; then
    less -R "$output"
  else
    cat "$output"
  fi
  rm -f "$output"
}

while true; do
  clear
  echo "Google Maps 評論監控"
  echo "===================="
  echo "1. 顯示評論內容"
  echo "2. 前回執行結果與下次執行時間"
  echo "3. 查看日期推算證據"
  echo "0. 離開"
  echo
  read -r -p "請輸入選項：" choice

  case "$choice" in
    1) show_reviews ;;
    2) show_schedule ;;
    3) show_date_evidence ;;
    0) exit 0 ;;
    *) echo "無效選項。" ;;
  esac

  echo
  read -r -p "按 Enter 返回主選單..." _
done
