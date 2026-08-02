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
