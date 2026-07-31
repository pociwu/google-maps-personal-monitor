from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from .database import Database


EVENT_LABELS = {
    "new": "🆕 新增評論",
    "modified": "✏️ 評論已修改",
    "deleted": "🗑️ 評論已刪除",
    "restored": "♻️ 評論已恢復",
    "baseline_summary": "✅ 基準匯入完成",
    "target_failure": "⚠️ 個人頁面連續抓取失敗",
    "target_recovered": "✅ 個人頁面恢復正常",
    "target_disabled": "⏸️ 已停用監控對象",
    "system_failure": "🚨 監控程式執行失敗",
    "disk_low": "💽 磁碟空間不足",
    "disk_recovered": "✅ 磁碟空間恢復",
    "health_summary": "📊 每日監控摘要",
    "image_failure": "🖼️ 評論圖片保存失敗",
    "date_changed": "🗓️ 評論日期推算更新",
    "date_observation_summary": "📅 日期觀察期摘要",
    "date_model_calibrated": "✅ 日期模型校準完成",
    "date_model_invalid": "⚠️ 日期模型已失效",
    "test": "✅ Telegram 測試成功",
}

CONFIRMED_DATE_CONFIDENCE = {"confirmed_time", "confirmed_date"}


def format_event(event_type: str, payload: dict[str, Any]) -> str:
    lines = [EVENT_LABELS.get(event_type, event_type)]
    if payload.get("target_name"):
        lines.append(f"人物：{payload['target_name']}")
    if payload.get("place_name"):
        lines.append(f"店家：{payload['place_name']}")
    if payload.get("rating") is not None:
        lines.append(f"星等：{payload['rating']:g}")
    if payload.get("publish_date"):
        lines.append(f"推算發表日期：{payload['publish_date']}")
    if payload.get("publish_estimate"):
        lines.append(f"推算時間：{payload['publish_estimate']}")
    if payload.get("confidence"):
        lines.append(f"可信度：{payload['confidence']}")
    if payload.get("basis"):
        lines.append(f"推算依據：{payload['basis']}")
    if payload.get("time_subject"):
        lines.append(f"時間主體：{payload['time_subject']}")
    if payload.get("publish_earliest") and payload.get("publish_latest"):
        lines.append(f"可能區間：{payload['publish_earliest']} ～ {payload['publish_latest']}")
    if payload.get("confirmed_count") is not None:
        lines.append(
            f"已確認 {payload['confirmed_count']}；高可信／推算 {payload.get('estimated_count', 0)}；"
            f"無法確認 {payload.get('unrecoverable_count', 0)}"
        )
    if payload.get("model_unit"):
        lines.append(f"模型單位：{payload['model_unit']}　版本：{payload.get('model_version', '-')}")
    if payload.get("edit_date"):
        lines.append(f"推算修改日期：{payload['edit_date']}")
    if payload.get("photo_count") is not None:
        lines.append(f"圖片：{payload['photo_count']} 張")
    if payload.get("image_added_count") or payload.get("image_removed_count"):
        lines.append(
            f"圖片變更：新增 {payload.get('image_added_count', 0)} 張；"
            f"移除 {payload.get('image_removed_count', 0)} 張"
        )
    if payload.get("review_count") is not None:
        lines.append(f"評論：{payload['review_count']} 則")
    if payload.get("saved_image_count") is not None:
        lines.append(f"已保存圖片：{payload['saved_image_count']} 張")
    if payload.get("observation_hours") is not None:
        lines.append(f"觀察模式：{payload['observation_hours']} 小時")
    if payload.get("target_count") is not None:
        lines.append(
            f"監控 {payload['target_count']} 人；成功 {payload.get('successes', 0)}；失敗 {payload.get('failures', 0)}"
        )
    if payload.get("consecutive_failures"):
        lines.append(f"連續失敗：{payload['consecutive_failures']} 輪")
    message = payload.get("message")
    if message:
        lines.append(str(message))
    text = payload.get("text")
    if text:
        lines.extend(["", str(text)])
    error = payload.get("error")
    if error:
        lines.extend(["", f"錯誤：{error}"])
    url = payload.get("review_url") or payload.get("place_url") or payload.get("target_url")
    if url:
        lines.extend(["", str(url)])
    result = "\n".join(lines)
    return result if len(result) <= 4096 else result[:4050] + "\n…（內容已截短）"


class TelegramSender:
    def __init__(self, db: Database, token: str, chat_id: str, delay: tuple[int, int]):
        self.db = db
        self.token = token
        self.chat_id = chat_id
        self.delay = delay

    async def send_pending(self) -> tuple[int, int]:
        sent = 0
        failed = 0
        events = []
        for event in self.db.get_pending_events():
            payload = json.loads(event["payload_json"])
            if (
                event["event_type"] == "date_changed"
                and payload.get("confidence") not in CONFIRMED_DATE_CONFIDENCE
            ):
                self.db.mark_event_suppressed(
                    event["id"], "日期更新尚未達高可信，不發送 Telegram"
                )
                continue
            events.append((event, payload))
        async with httpx.AsyncClient(timeout=30.0) as client:
            for index, (event, payload) in enumerate(events):
                if index:
                    await asyncio.sleep(random.randint(*self.delay))
                self.db.mark_event_attempted(event["id"])
                try:
                    response = await client.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={"chat_id": self.chat_id, "text": format_event(event["event_type"], payload)},
                    )
                except httpx.HTTPError as exc:
                    self.db.connection.execute(
                        "UPDATE events SET last_error=? WHERE id=?",
                        (f"傳送結果不明：{exc}"[:2000], event["id"]),
                    )
                    self.db.connection.commit()
                    failed += 1
                    continue
                try:
                    body = response.json()
                except ValueError:
                    self.db.connection.execute(
                        "UPDATE events SET last_error=? WHERE id=?",
                        (f"傳送結果不明：HTTP {response.status_code}", event["id"]),
                    )
                    self.db.connection.commit()
                    failed += 1
                    continue
                if response.is_success and body.get("ok") is True:
                    message_id = body.get("result", {}).get("message_id")
                    self.db.mark_event_sent(event["id"], message_id)
                    sent += 1
                else:
                    attempts = int(event["attempts"]) + 1
                    error = body.get("description") or f"HTTP {response.status_code}"
                    self.db.mark_event_explicit_failure(event["id"], error, retry=attempts < 3)
                    failed += 1
        return sent, failed
