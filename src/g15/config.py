"""Configuration model and utilities for g15 predictive maintenance sandbox.

Provides:
- Config dataclass with validation rules
- YAML loading helpers
- sha256_of_file utility
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(Exception):
    """Raised when a configuration fails validation."""


@dataclass
class Config:
    raw: Dict[str, Any]
    schema_version: str
    record_fields: List[str]
    manifest: Dict[str, Any]
    checksum_field: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        schema_version = data.get("schema_version")
        record_fields = data.get("record_fields")
        manifest = data.get("manifest", {})
        checksum_field = data.get("checksum_field")
        cfg = cls(raw=data, schema_version=schema_version, record_fields=record_fields, manifest=manifest, checksum_field=checksum_field)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Validate the loaded configuration according to project rules.

        Rules enforced:
        - schema_version must be exactly "1.0"
        - record_fields must be a list containing at least the required fields
          ['seq', 'timestamp_ns', 'vibration', 'temperature']
        - manifest.require_complete must be True
        - checksum_field, if present, must be a string
        """
        if self.schema_version != "1.0":
            raise ConfigError(f"unsupported schema_version: {self.schema_version!r}, expected '1.0'")

        if not isinstance(self.record_fields, list):
            raise ConfigError("record_fields must be a YAML list")

        required = {"seq", "timestamp_ns", "vibration", "temperature"}
        missing = required - set(self.record_fields)
        if missing:
            raise ConfigError(f"record_fields is missing required fields: {sorted(list(missing))}")

        if not isinstance(self.manifest, dict):
            raise ConfigError("manifest must be a mapping/dict")

        require_complete = self.manifest.get("require_complete")
        if require_complete is not True:
            raise ConfigError("manifest.require_complete must be true")

        if self.checksum_field is not None and not isinstance(self.checksum_field, str):
            raise ConfigError("checksum_field must be a string if present")


def load_yaml(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError("root of config YAML must be a mapping")
    return data


def load_config(path: str | Path) -> Config:
    data = load_yaml(path)
    return Config.from_dict(data)


def sha256_of_file(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
