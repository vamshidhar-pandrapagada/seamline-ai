"""Map a folder to a service: longest matching service path wins, else the integration scope."""

from __future__ import annotations

from pathlib import Path

from seamline.config import INTEGRATION, Config


def service_for(folder: Path | str, config: Config) -> str | None:
    """Return the service a folder belongs to.

    - inside a listed service folder (at any depth): that service, longest path first
    - the project root or an unlisted folder: "integration"
    - outside the project: None
    """
    target = Path(folder).resolve()
    if not target.is_relative_to(config.root):
        return None
    best: tuple[int, str] | None = None
    for name, service_dir in config.service_dirs().items():
        # Compare whole path components, so "api" never matches "api-gateway".
        if target.is_relative_to(service_dir):
            depth = len(service_dir.parts)
            if best is None or depth > best[0]:
                best = (depth, name)
    return best[1] if best else INTEGRATION
