import sys
from pathlib import Path

# Ensure the package under src/ is importable when tests run from the repository root
ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tempfile
import yaml
import pytest

from g15.config import load_config, ConfigError


def test_valid_config_loads():
    root = Path(__file__).resolve().parents[3]
    cfg_path = root / "configs" / "default.yaml"
    cfg = load_config(cfg_path)
    assert cfg.schema_version == "1.0"


def test_missing_record_fields_fails(tmp_path):
    p = tmp_path / "cfg.yaml"
    data = {"schema_version": "1.0", "manifest": {"require_complete": True}}
    p.write_text(yaml.dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_wrong_schema_version_fails(tmp_path):
    p = tmp_path / "cfg.yaml"
    data = {"schema_version": "2.0", "record_fields": ["seq", "timestamp_ns", "vibration", "temperature"], "manifest": {"require_complete": True}}
    p.write_text(yaml.dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_require_complete_must_be_true(tmp_path):
    p = tmp_path / "cfg.yaml"
    data = {"schema_version": "1.0", "record_fields": ["seq", "timestamp_ns", "vibration", "temperature"], "manifest": {"require_complete": False}}
    p.write_text(yaml.dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_checksum_field_must_be_string(tmp_path):
    p = tmp_path / "cfg.yaml"
    data = {"schema_version": "1.0", "record_fields": ["seq", "timestamp_ns", "vibration", "temperature"], "manifest": {"require_complete": True}, "checksum_field": 123}
    p.write_text(yaml.dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
