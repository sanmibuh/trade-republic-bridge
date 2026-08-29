"""Configuration — reads a YAML config file.

No other module may call ``os.getenv`` or read the config file directly;
all configuration must come through this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_INSTANCE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_DEFAULT_CONFIG_PATH = "/data/config.yml"
_CONFIG_PATH_ENV = "TR_CONFIG_PATH"


def _resolve_config_path() -> str:
    """Resolve the config file path, honouring the ``TR_CONFIG_PATH`` override.

    Defaults to the container path ``/data/config.yml``; local development can
    point it elsewhere via the ``TR_CONFIG_PATH`` environment variable.
    """
    return os.getenv(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)


CONFIG_PATH = _resolve_config_path()
_DEFAULT_DATA_ROOT = "/data"
_DEFAULT_TFA_TIMEOUT = 120


class ConfigError(Exception):
    """Raised when the config file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class InstanceConfig:
    """Configuration for a single Trade Republic account instance."""

    name: str
    phone: str
    pin: str


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of the service configuration."""

    api_key: str
    instances: list[InstanceConfig]
    tfa_timeout: int = _DEFAULT_TFA_TIMEOUT
    data_root: str = _DEFAULT_DATA_ROOT

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> Config:
        """Load config from the resolved path (``TR_CONFIG_PATH`` or default)."""
        return cls.from_file()

    @classmethod
    def from_file(cls, path: str = CONFIG_PATH) -> Config:
        """Load and validate config from *path*.

        The data root (where ``tr_session_{name}/`` directories live) is the
        directory that contains the config file, matching the documented
        ``/data`` layout while remaining relocatable for local development.

        Raises:
            ConfigError: if the file is missing or the content is invalid.
        """
        raw = cls._read_yaml(path)
        api_key = cls._require_str(raw, "api_key")
        instances = cls._parse_instances(raw)
        tfa_timeout = cls._parse_tfa_timeout(raw)
        data_root = str(Path(path).resolve().parent)
        return cls(
            api_key=api_key,
            instances=instances,
            tfa_timeout=tfa_timeout,
            data_root=data_root,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def instance_names(self) -> list[str]:
        """Ordered list of instance names."""
        return [i.name for i in self.instances]

    def session_dir(self, name: str) -> str:
        """Return the session directory path for a configured instance *name*.

        The data root is the directory containing the config file (``/data``
        inside the container); operators control its location via the Docker
        volume mount or, locally, via ``TR_CONFIG_PATH``.

        Raises:
            ConfigError: if *name* is not a configured instance.
        """
        if name not in self.instance_names:
            raise ConfigError(f"Unknown instance name: {name!r}")
        return f"{self.data_root}/tr_session_{name}"

    # ------------------------------------------------------------------
    # Private parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_yaml(path: str) -> dict:
        try:
            text = Path(path).read_text()
        except OSError as exc:
            raise ConfigError(f"Config file not found: {path!r}") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in config file: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(
                "Config file must be a YAML mapping at the top level; "
                f"got: {type(data).__name__}"
            )
        return data

    @staticmethod
    def _require_str(raw: dict, key: str) -> str:
        value = raw.get(key, "")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"Config field {key!r} is required and must be a non-empty string"
            )
        return value.strip()

    @classmethod
    def _parse_instances(cls, raw: dict) -> list[InstanceConfig]:
        items = raw.get("instances")
        if not isinstance(items, list) or not items:
            raise ConfigError(
                "Config field 'instances' is required and must be a non-empty list"
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ConfigError(
                    f"Each entry in 'instances' must be a mapping; "
                    f"entry {i} is {type(item).__name__}"
                )
        instances = [cls._parse_instance(item) for item in items]
        names = [inst.name for inst in instances]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ConfigError(f"Duplicate instance name: {name!r}")
            seen.add(name)
        return instances

    @classmethod
    def _parse_tfa_timeout(cls, raw: dict) -> int:
        value = raw.get("tfa_timeout", _DEFAULT_TFA_TIMEOUT)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(
                "Config field 'tfa_timeout' must be a positive integer (seconds)"
            )
        return value

    @classmethod
    def _parse_instance(cls, item: dict) -> InstanceConfig:
        name = cls._require_instance_field(item, "name")
        phone = cls._require_instance_field(item, "phone")
        pin = cls._require_instance_field(item, "pin")
        cls._validate_instance_name(name)
        return InstanceConfig(name=name, phone=phone, pin=pin)

    @staticmethod
    def _require_instance_field(item: dict, key: str) -> str:
        value = item.get(key, "")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"Each instance must have a non-empty {key!r} field")
        return value.strip()

    @staticmethod
    def _validate_instance_name(name: str) -> None:
        if not _INSTANCE_NAME_RE.match(name):
            raise ConfigError(
                f"Invalid instance name {name!r}: must match ^[a-zA-Z0-9_-]+$"
            )
