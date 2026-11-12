from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

INVENTORY = """
mikrotik:
  - name: crs-core
    host: crs.lab
    role: core switch/router
    expected_up: [sfp-sfpplus1]
    protected_interfaces: [sfp-sfpplus1, ether1]
  - name: ap-office
    host: ap.lab
snmp:
  - name: usw-garage
    host: 10.0.0.30
    kind: ubiquiti
technitium:
  url: http://dns.lab:5380
  dns_server: 10.0.0.53
kea:
  url: http://dhcp.lab:8000
proxmox:
  url: https://pve.lab:8006
truenas:
  url: wss://nas.lab/api/current
prometheus:
  url: http://prom.lab:9090
  alertmanager_url: http://am.lab:9093
zabbix:
  url: http://zbx.lab
graylog:
  url: http://graylog.lab:9000
ssh_hosts:
  - name: dhcp1
    host: dhcp.lab
    services: [isc-kea-dhcp4-server]
    containers: [graylog]
"""


@pytest.fixture(autouse=True)
def lab(tmp_path, monkeypatch):
    from labtools import common

    inv = tmp_path / "inventory.yaml"
    inv.write_text(INVENTORY)
    monkeypatch.setenv("LAB_INVENTORY", str(inv))
    for k, v in {"MIKROTIK_USER": "watch", "MIKROTIK_PASSWORD": "pw", "TECHNITIUM_TOKEN": "t",
                 "PROXMOX_TOKEN": "watch@pve!mon=abc", "TRUENAS_API_KEY": "k", "ZABBIX_TOKEN": "z",
                 "GRAYLOG_TOKEN": "g"}.items():
        monkeypatch.setenv(k, v)
    common.inventory.cache_clear()
    yield
    common.inventory.cache_clear()
