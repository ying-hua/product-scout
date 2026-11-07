"""Source plugins. Each module exposes `NAME` and `fetch(http, cfg) -> list[Item]`.

To add a source: create a module here, then add it to REGISTRY. A source with
`enabled = false` (or missing credentials) is skipped.
"""

from __future__ import annotations

from types import ModuleType

from . import github, hackernews, huggingface

REGISTRY: dict[str, ModuleType] = {
    github.NAME: github,
    huggingface.NAME: huggingface,
    hackernews.NAME: hackernews,
}
