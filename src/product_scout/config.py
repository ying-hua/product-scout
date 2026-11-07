"""Load the TOML config, merged over the built-in defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

CONFIG_NAME = "product-scout.toml"


def default_config_text() -> str:
    return resources.files("product_scout").joinpath("defaults/config.toml").read_text(encoding="utf-8")


def default_prompt_text(name: str) -> str:
    return resources.files("product_scout").joinpath(f"defaults/prompts/{name}").read_text(encoding="utf-8")


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; never overrides variables already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    def section(self, *path: str) -> dict[str, Any]:
        node: Any = self.data
        for p in path:
            node = node.get(p, {}) if isinstance(node, dict) else {}
        return node if isinstance(node, dict) else {}

    @property
    def data_dir(self) -> Path:
        return (self.root / self.data["data"]["dir"]).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"

    def prompt(self, name: str) -> str:
        custom = self.root / self.data["data"].get("prompts_dir", "prompts") / name
        if custom.is_file():
            return custom.read_text(encoding="utf-8")
        return default_prompt_text(name)


def find_config(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        candidate = d / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> Config:
    defaults = tomllib.loads(default_config_text())
    if path is None:
        path = find_config(Path.cwd())
    if path is None:
        # No config file: run with defaults in the current directory.
        root = Path.cwd()
        load_dotenv(root / ".env")
        return Config(root=root, data=defaults)
    path = Path(path).resolve()
    with path.open("rb") as f:
        user = tomllib.load(f)
    load_dotenv(path.parent / ".env")
    return Config(root=path.parent, data=_merge(defaults, user))
