from seamline.extract.pricing import estimate_usd, price_for, usd_for


def test_estimate_matches_measured_opus_runs():
    # Real excerpts: ~5.1k chars each; measured $0.043 and $0.049 on Opus 5
    est = estimate_usd("claude-opus-5", 5_100, 1)
    assert 0.03 < est < 0.07


def test_estimate_scales_and_handles_unknown_models():
    assert estimate_usd("claude-haiku-4-5", 20_000, 2) < estimate_usd("claude-opus-5", 20_000, 2)
    assert estimate_usd("claude-opus-5", 0, 0) == 0.0
    assert estimate_usd("some-future-model", 5_000, 1) is None


def test_dated_model_ids_use_their_alias_price():
    assert price_for("claude-haiku-4-5-20251001") == price_for("claude-haiku-4-5")
    assert price_for("claude-opus-50") is None  # A prefix match needs the "-" boundary
    assert usd_for("claude-opus-5", 1_000_000, 100_000) == 5.0 + 2.5
    assert usd_for("fake", 10, 10) is None
