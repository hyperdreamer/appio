"""Fixed, opt-in service artifact; no installation or real manager is used."""
from pathlib import Path
import tomllib


def test_service_is_root_owned_runtime_with_no_gui_or_bpf_dependency():
    root = Path(__file__).parents[2]
    unit = (root / "data/systemd/appio-iod.service").read_text()
    assert "ExecStart=/usr/bin/appio-iod" in unit
    assert "User=root" in unit
    assert "UMask=0077" in unit
    assert "StateDirectory=appio" in unit
    assert "RuntimeDirectory=appio" in unit
    assert "PrivateUsers=yes" not in unit
    assert "ProcSubset=pid" not in unit
    assert "ConditionPathExists=/sys/kernel/btf" not in unit
    scripts = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["appio-iod"] == "appio.iod:main"


def test_service_is_disabled_by_default_and_has_a_finite_stop_budget():
    root = Path(__file__).parents[2]
    unit = (root / "data/systemd/appio-iod.service").read_text()
    for setting in ("Group=appio", "StateDirectoryMode=0711", "RuntimeDirectoryMode=0750",
                    "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                    "NoNewPrivileges=yes", "Restart=on-failure", "TimeoutStopSec=5s"):
        assert setting in unit
    assert not list((root / "data/systemd").glob("*.socket"))
    assert not list((root / "data/systemd").glob("*.wants/*"))
    assert not any(word in unit.lower() for word in ("bpf", "display=", "appio.ui", "execstartpost"))
