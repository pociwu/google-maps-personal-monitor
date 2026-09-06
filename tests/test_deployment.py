from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_web_container_is_read_only_and_publishes_port_8000():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    web = compose["services"]["web"]

    assert web["read_only"] is True
    assert web["shm_size"] == "512mb"
    assert web["tmpfs"] == ["/tmp:size=512m,mode=1777"]
    assert web["ports"] == ["0.0.0.0:8000:8000"]
    assert "./state/web:/app/state/web:ro" in web["volumes"]
    assert "./state/data/images:/app/state/data/images:ro" in web["volumes"]
    assert "./config:/app/config:rw" in web["volumes"]
    assert "./state:/app/state:ro" not in web["volumes"]
    assert web["cap_drop"] == ["ALL"]
    assert "--no-access-log" in web["command"]
    assert web["restart"] == "unless-stopped"


def test_deployment_scripts_do_not_modify_firewall():
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "deploy").glob("*.sh")
    ).lower()
    assert "ufw " not in scripts
    assert "iptables" not in scripts
    assert "firewall-cmd" not in scripts


def test_systemd_failure_notification_identifies_the_real_source_unit():
    systemd = ROOT / "deploy" / "systemd"
    for name in (
        "maps-monitor.service",
        "maps-monitor-dense.service",
        "maps-monitor-backup.service",
    ):
        unit = (systemd / name).read_text(encoding="utf-8")
        assert "OnFailure=maps-monitor-failure@%n.service" in unit

    handler = (systemd / "maps-monitor-failure@.service").read_text(encoding="utf-8")
    assert "notify-system-failure --source-unit %i" in handler
    assert not (systemd / "maps-monitor-failure.service").exists()


def test_dense_dispatcher_checks_often_enough_for_hour_precision():
    timer = (
        ROOT / "deploy" / "systemd" / "maps-monitor-dense.timer"
    ).read_text(encoding="utf-8")

    assert "OnUnitInactiveSec=5min" in timer
    assert "RandomizedDelaySec=1min" in timer
    assert "AccuracySec=1s" in timer


def test_backup_can_wait_longer_than_the_maximum_normal_crawl():
    service = (
        ROOT / "deploy" / "systemd" / "maps-monitor-backup.service"
    ).read_text(encoding="utf-8")

    assert "TimeoutStartSec=6h" in service
