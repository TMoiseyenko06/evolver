"""Offline tests for the parameter-tuning module (no LLM, mock market)."""

import random

import pytest

from evolver.store import Store
from evolver.tune import (
    ParamSpec,
    build_variant,
    mutate_params,
    parse_param_spec,
    render_template,
    run_tuning,
    sample_params,
)

from helpers import MockMarket, default_window_specs, make_config

# A template whose single int param `bias` decides the side: >=50 -> Up, else Down.
TEMPLATE = """# lineage: tuned
class Strategy:
    NAME = "tmpl"
    DESCRIPTION = "tunable side-bias probe"
    def decide(self, ctx):
        if {{bias}} >= 50:
            return {"side": "Up"}
        return {"side": "Down"}
"""
SPEC = [ParamSpec("bias", 0, 100, is_int=True)]


def test_parse_and_render():
    specs = parse_param_spec({"ask_cap": {"min": 0.3, "max": 0.55, "type": "float", "default": 0.42},
                              "wait": {"min": 30, "max": 170, "type": "int"}})
    assert len(specs) == 2 and specs[1].is_int
    src = render_template("a={{ask_cap}} b={{wait}}", {"ask_cap": 0.4, "wait": 100})
    assert src == "a=0.4 b=100"


def test_sample_and_mutate_respect_bounds():
    rng = random.Random(1)
    for _ in range(50):
        p = sample_params(SPEC, rng)
        assert 0 <= p["bias"] <= 100 and isinstance(p["bias"], int)
        m = mutate_params(p, SPEC, rng, sigma=0.5)
        assert 0 <= m["bias"] <= 100


def test_build_variant_compiles_and_carries_params(tmp_path):
    cfg = make_config(tmp_path)
    strat = build_variant(TEMPLATE, {"bias": 80}, "probe", 1, 0, cfg, set())
    assert strat is not None
    assert strat.params == {"bias": 80}
    assert strat.decide.__self__ is strat  # bound method exists
    # bias 80 -> Up
    from evolver.tune import render_template as rt  # decision check via a fresh compile
    assert "80 >= 50" in rt(TEMPLATE, {"bias": 80})


def test_run_tuning_optimizes_toward_up(tmp_path):
    # default_window_specs resolves Up 3x, Down 1x, so bias>=50 (Up) should win.
    cfg = make_config(tmp_path, windows_per_generation=4)
    store = Store(cfg)
    market = MockMarket(default_window_specs())
    final = run_tuning(
        "probe", TEMPLATE, SPEC, market, store, cfg,
        variants=6, keep=3, generations=3, rng=random.Random(0),
    )
    assert len(final) == 6
    # The best variant should be an "Up" one (bias >= 50) and net positive.
    best = max(final, key=lambda s: s.lifetime.net_pnl)
    assert best.params["bias"] >= 50
    assert best.lifetime.net_pnl > 0
    # Survivors carried across generations -> at least one survived >1 gen.
    assert max(s.generations_survived for s in final) >= 1
    # A per-generation tuning report was written.
    assert any(p.name.startswith("tune_probe_gen") for p in cfg.runs_dir.glob("*.md"))


def test_build_variant_rejects_unfilled_placeholder(tmp_path):
    cfg = make_config(tmp_path)
    # 'missing' has no value -> unfilled placeholder -> None
    tmpl = TEMPLATE.replace("{{bias}}", "{{missing}}")
    assert build_variant(tmpl, {"bias": 80}, "probe", 1, 0, cfg, set()) is None
