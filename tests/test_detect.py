from seamline.detect import detect_contracts, detect_services, suggest_names


def names_and_paths(services):
    return {(s.name, s.path) for s in services}


def test_monorepo_two_levels(make_tree):
    root = make_tree(
        "apps/web/package.json",
        "apps/api/pyproject.toml",
        "packages/billing/go.mod",
        "docs/readme.md",
    )
    assert names_and_paths(detect_services(root)) == {
        ("web", "apps/web"),
        ("api", "apps/api"),
        ("billing", "packages/billing"),
    }


def test_markers(make_tree):
    root = make_tree(
        "a/Cargo.toml", "b/pom.xml", "c/Dockerfile", "d/Dockerfile.dev", "e/requirements.txt"
    )
    found = {s.path: s.marker for s in detect_services(root)}
    assert found == {"a": "Cargo.toml", "b": "pom.xml", "c": "Dockerfile"}


def test_too_deep_is_ignored(make_tree):
    root = make_tree("one/two/three/package.json")
    assert detect_services(root) == []


def test_skip_dirs(make_tree):
    root = make_tree(
        "node_modules/lib/package.json",
        ".venv/pkg/pyproject.toml",
        "vendor/x/go.mod",
        "target/debug/Cargo.toml",
        "dist/app/package.json",
        "build/app/package.json",
        ".git/hooks/package.json",
        "real/package.json",
    )
    assert [s.path for s in detect_services(root)] == ["real"]


def test_service_not_searched_below(make_tree):
    root = make_tree("web/package.json", "web/packages/ui/package.json")
    assert [s.path for s in detect_services(root)] == ["web"]


def test_root_marker_is_not_a_service(make_tree):
    root = make_tree("pyproject.toml", "src/app.py")
    assert detect_services(root) == []


def test_contracts(make_tree):
    root = make_tree(
        "proto/agents/agent.proto",
        "proto/agents/event.proto",
        "docker/docker-compose.yml",
        "compose.override.yaml",
        "api/openapi.yaml",
        "services/orders/pyproject.toml",
        "services/orders/proto/orders.proto",
        "node_modules/x/proto/y.proto",
        "notes.yml",
    )
    found = {(c.kind, c.path) for c in detect_contracts(root)}
    assert found == {
        ("proto", "proto"),
        ("proto", "services/orders/proto"),
        ("compose", "docker/docker-compose.yml"),
        ("compose", "compose.override.yaml"),
        ("openapi", "api/openapi.yaml"),
    }


def test_proto_folder_with_own_files_stays(make_tree):
    root = make_tree("contracts/README.md", "contracts/v1/a.proto")
    assert [c.path for c in detect_contracts(root)] == ["contracts/v1"]


def test_suggest_names_shared_prefix():
    assert suggest_names(["agent-core", "agent-orchestrator"]) == ["core", "orchestrator"]


def test_suggest_names_service_suffix():
    assert suggest_names(["orders-service", "payments_svc"]) == ["orders", "payments"]


def test_suggest_names_collisions():
    assert suggest_names(["apps/api", "services/api"]) == ["apps-api", "services-api"]


def test_suggest_names_single_keeps_prefix():
    assert suggest_names(["agent-core"]) == ["agent-core"]


def test_suggest_names_slug_and_reserved():
    assert suggest_names(["My App", "integration"]) == ["my-app", "integration-service"]
