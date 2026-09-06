from maps_monitor import cli
from maps_monitor.config import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        root=tmp_path,
        data_dir=tmp_path / "state" / "data",
        image_dir=tmp_path / "state" / "data" / "images",
        backup_dir=tmp_path / "state" / "backups",
        debug_dir=tmp_path / "state" / "debug",
        database=tmp_path / "state" / "data" / "monitor.sqlite3",
        targets=(),
        timezone="Asia/Taipei",
        locale="zh-TW",
        observation_hours=24,
        profile_delay_seconds=(0, 0),
        telegram_delay_seconds=(0, 0),
        delete_after_missing_runs=3,
        disk_min_free_gb=0,
        disk_min_free_percent=0,
        max_profile_minutes=1,
        telegram_token="token",
        telegram_chat_id="chat",
    )


def test_dense_schedule_collision_is_a_successful_noop(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)

    with cli._process_lock(settings):
        assert cli.main(["dense-run-and-send"]) == 0

    assert not settings.database.exists()
    assert not (tmp_path / "state" / "web" / "monitor.sqlite3").exists()
