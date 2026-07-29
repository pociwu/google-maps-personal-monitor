from pathlib import Path

import pytest

from maps_monitor.config import load_settings


def _write(path: Path, targets: str) -> Path:
    config = path / "config"
    config.mkdir()
    target_file = config / "targets.yaml"
    target_file.write_text("timezone: Asia/Taipei\ntargets:\n" + targets, encoding="utf-8")
    return target_file


def test_accepts_public_contributor_url(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_MONITOR_DATA_DIR", str(tmp_path / "state"))
    path = _write(
        tmp_path,
        '  - name: 測試\n    url: "https://www.google.com/maps/contrib/123/reviews"\n',
    )
    settings = load_settings(path)
    assert settings.targets[0].name == "測試"


def test_rejects_more_than_ten_targets(tmp_path):
    items = "".join(
        f'  - name: u{i}\n    url: "https://www.google.com/maps/contrib/{i}/reviews"\n'
        for i in range(11)
    )
    with pytest.raises(ValueError, match="10"):
        load_settings(_write(tmp_path, items))


def test_rejects_non_contributor_url(tmp_path):
    path = _write(tmp_path, '  - name: bad\n    url: "https://example.com/profile"\n')
    with pytest.raises(ValueError, match="貢獻者"):
        load_settings(path)


def test_rejects_duplicate_target_names(tmp_path):
    path = _write(
        tmp_path,
        """  - name: 相同
    url: "https://www.google.com/maps/contrib/1/reviews"
  - name: 相同
    url: "https://www.google.com/maps/contrib/2/reviews"
""",
    )
    with pytest.raises(ValueError, match="名稱"):
        load_settings(path)
