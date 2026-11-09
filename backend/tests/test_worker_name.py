from app import config


def _setup(monkeypatch, tmp_path, hostname, *, container=True, host_file=None, **env):
    for k in ("WORKER_NAME", "WORKER_PREFIX", "HOST_NAME"):
        monkeypatch.delenv(f"AHQ_{k}", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(f"AHQ_{k}", v)
    monkeypatch.setattr(config.socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(config, "_in_container", lambda h: container)
    f = tmp_path / "host_hostname"
    if host_file is not None:
        f.write_text(host_file)
    monkeypatch.setattr(config, "HOST_HOSTNAME_FILE", f)


def test_container_name_is_prefix_host_and_short_id(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, "451a164d7090", host_file="shockwave.home.arpa\n")
    assert config.default_worker_name() == "worker-shockwave-7090"


def test_replicas_differ_and_prefix_is_per_service(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, "9f0c2b1e3a55", host_file="shockwave", WORKER_PREFIX="hq")
    assert config.default_worker_name() == "hq-shockwave-3a55"


def test_host_name_from_env_when_no_file(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, "451a164d7090", HOST_NAME="einstein")
    assert config.default_worker_name() == "worker-einstein-7090"
    _setup(monkeypatch, tmp_path, "451a164d7090")
    assert config.default_worker_name() == "worker-7090"


def test_explicit_name_and_bare_metal_unchanged(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, "451a164d7090", host_file="shockwave", WORKER_NAME="worker-desktop")
    assert config.default_worker_name() == "worker-desktop"
    _setup(monkeypatch, tmp_path, "computron", container=False)
    assert config.default_worker_name() == "computron"
