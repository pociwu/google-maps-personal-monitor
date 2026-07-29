from maps_monitor.config import TargetConfig
from maps_monitor.database import Database


def test_disable_notifies_once_and_reenable_requires_silent_sync(tmp_path):
    db = Database(tmp_path / "monitor.sqlite3")
    enabled = (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews", True),)
    disabled = (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews", False),)
    target = db.sync_targets(enabled, 24)[0]
    db.connection.execute("UPDATE targets SET baseline_complete=1 WHERE id=?", (target["id"],))
    db.connection.commit()

    assert db.sync_targets(disabled, 24) == []
    db.sync_targets(disabled, 24)
    assert db.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='target_disabled'"
    ).fetchone()[0] == 1

    reenabled = db.sync_targets(enabled, 24)[0]
    assert reenabled["baseline_complete"] == 0
    db.close()
