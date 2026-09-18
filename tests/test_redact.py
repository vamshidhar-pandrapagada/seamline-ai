import pytest

from seamline.transcripts.redact import MASK, is_env_file, redact, redact_value


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-" + "A" * 40,
        "sk-proj-" + "b" * 40,
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "c" * 36,
        "github_pat_" + "d" * 60,
        "xoxb-1234567890-abcdef",
        "AIza" + "e" * 35,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    ],
)
def test_tokens(secret):
    out = redact(f"before {secret} after")
    assert secret not in out
    assert out == f"before {MASK} after"


def test_private_key_block():
    text = "x\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\ny"
    assert redact(text) == f"x\n{MASK}\ny"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("DB_PASSWORD=hunter22", f"DB_PASSWORD={MASK}"),
        ('"api_key": "abcd1234efgh"', f'"api_key": "{MASK}"'),
        ("export GITHUB_TOKEN=abcdef123", f"export GITHUB_TOKEN={MASK}"),
        ("STRIPE_KEY=live_abc123", f"STRIPE_KEY={MASK}"),
        ("client_secret: s3cr3tvalue", f"client_secret: {MASK}"),
        ("postgres://app:pa55word@db:5432/x", f"postgres://app:{MASK}@db:5432/x"),
        ("Authorization: Bearer abcdefghijklmnopqrstuv", f"Authorization: Bearer {MASK}"),
    ],
)
def test_assignments_and_urls(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens = 400",
        "update_max_tokens: 100",
        "the tokenizer splits words",
        "order.created carries amount_cents as an int",
        "commit 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f",
        "session 3f1c2a9e-7b4d-4e21-9a6f-0c8d5e2b7a14",
        "https://example.com/path?x=1",
        "",
    ],
)
def test_leaves_normal_text(text):
    assert redact(text) == text


def test_redact_value_nested():
    value = {
        "command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuv'",
        "n": 3,
        "list": ["DB_PASSWORD=hunter22"],
    }
    out = redact_value(value)
    assert MASK in out["command"] and out["n"] == 3 and out["list"] == [f"DB_PASSWORD={MASK}"]


@pytest.mark.parametrize(
    "path, expected",
    [
        (".env", True),
        ("/a/.env.local", True),
        ("prod.env", True),
        ("envs.py", False),
        ("/a/environment.md", False),
        (None, False),
    ],
)
def test_is_env_file(path, expected):
    assert is_env_file(path) is expected
