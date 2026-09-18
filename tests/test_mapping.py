from pathlib import Path

import pytest

from seamline.config import parse_config
from seamline.mapping import service_for


def config(root: Path, services: dict[str, str]):
    return parse_config({"project": "p", "services": services}, root=root)


def test_monorepo(tmp_path):
    cfg = config(tmp_path, {"web": "apps/web", "api": "apps/api", "billing": "packages/billing"})
    assert service_for(tmp_path / "apps/web", cfg) == "web"
    assert service_for(tmp_path / "apps/api/src/routes", cfg) == "api"
    assert service_for(tmp_path / "packages/billing", cfg) == "billing"


def test_backend_frontend_split(tmp_path):
    cfg = config(tmp_path, {"backend": "backend", "frontend": "frontend"})
    assert service_for(tmp_path / "backend/app", cfg) == "backend"
    assert service_for(tmp_path / "frontend", cfg) == "frontend"


def test_polyrepo_parent(tmp_path):
    cfg = config(tmp_path, {"orders": "orders-service", "payments": "payments-service"})
    assert service_for(tmp_path / "orders-service/src", cfg) == "orders"
    assert service_for(tmp_path, cfg) == "integration"


def test_nested_services_longest_path_wins(tmp_path):
    cfg = config(tmp_path, {"api": "apps/api", "api-auth": "apps/api/auth"})
    assert service_for(tmp_path / "apps/api/auth/tokens", cfg) == "api-auth"
    assert service_for(tmp_path / "apps/api/routes", cfg) == "api"


def test_root_and_unlisted_folders_are_integration(tmp_path):
    cfg = config(tmp_path, {"web": "apps/web"})
    assert service_for(tmp_path, cfg) == "integration"
    assert service_for(tmp_path / "docs", cfg) == "integration"
    assert service_for(tmp_path / "apps", cfg) == "integration"


def test_outside_project(tmp_path):
    root = tmp_path / "shop"
    cfg = config(root, {"web": "web"})
    assert service_for(tmp_path, cfg) is None
    assert service_for(tmp_path / "other/web", cfg) is None
    assert service_for(tmp_path / "shop-old", cfg) is None  # prefix of the root name


@pytest.mark.parametrize("folder", ["api-gateway", "api-gateway/src", "apiv2"])
def test_path_prefix_trap(tmp_path, folder):
    cfg = config(tmp_path, {"api": "api"})
    assert service_for(tmp_path / folder, cfg) == "integration"


def test_relative_dotdot_resolves(tmp_path):
    cfg = config(tmp_path, {"web": "web"})
    assert service_for(tmp_path / "web/../web/src", cfg) == "web"
    assert service_for(tmp_path / "web/..", cfg) == "integration"


def test_symlinked_root(tmp_path):
    real = tmp_path / "real"
    (real / "web").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg = config(link, {"web": "web"})
    assert service_for(real / "web", cfg) == "web"
