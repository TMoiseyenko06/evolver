"""Tests for the AST sandbox: malicious code rejected, safe code runs, timeouts."""

import pytest

from evolver.sandbox import (
    SandboxError,
    StrategyTimeout,
    compile_strategy,
    run_with_timeout,
    validate,
)

from helpers import BODY_ALWAYS_UP, BODY_TIMEOUT, strategy_source

# --- malicious code must be rejected -------------------------------------- #
MALICIOUS = {
    "import os": "import os\nx = os.getcwd()",
    "import sys": "import sys",
    "from os import ...": "from os import system",
    "import requests": "import requests",
    "import socket": "import socket",
    "import subprocess": "import subprocess",
    "__import__": '__import__("os")',
    "exec": 'exec("x = 1")',
    "eval": 'eval("1+1")',
    "open file": 'open("/etc/passwd")',
    "dunder class": "().__class__.__bases__",
    "dunder globals": "(lambda: None).__globals__",
    "getattr escape": 'getattr(().__class__, "x")',
    "builtins access": "x = __builtins__",
    "compile": 'compile("1", "<s>", "eval")',
}


@pytest.mark.parametrize("label,src", list(MALICIOUS.items()), ids=list(MALICIOUS))
def test_malicious_rejected(label, src):
    with pytest.raises(SandboxError):
        validate(src)


def test_malicious_rejected_even_when_wrapped_in_strategy():
    src = strategy_source("evil", "import os\nreturn None")
    with pytest.raises(SandboxError):
        compile_strategy(src)


# --- safe code must pass -------------------------------------------------- #
def test_allowed_imports_ok():
    validate("import math\nimport statistics\nx = math.sqrt(2)")


def test_valid_strategy_compiles_and_runs():
    src = strategy_source("good", BODY_ALWAYS_UP)
    inst = compile_strategy(src)
    assert inst.NAME == "good"
    assert inst.decide(_FakeCtx()) == {"side": "Up"}


def test_strategy_can_use_math_and_statistics():
    body = """
import math
import statistics
vals = [c["close"] for c in ctx.candles] or [1.0]
if statistics.mean(vals) > 0 and math.sqrt(4) == 2:
    return {"side": "Up"}
return None
"""
    inst = compile_strategy(strategy_source("mathy", body))
    assert inst.decide(_FakeCtx()) in ({"side": "Up"}, None)


def test_missing_strategy_class_rejected():
    with pytest.raises(SandboxError):
        compile_strategy("x = 1")


def test_dangerous_builtins_absent_from_sandbox_namespace():
    # Defense in depth: even if validation were bypassed, the exec namespace has
    # no open/exec/eval/getattr/__import__-to-anything.
    from evolver.sandbox import _safe_builtins, DEFAULT_ALLOWED_IMPORTS

    b = _safe_builtins(DEFAULT_ALLOWED_IMPORTS)
    for name in ("open", "exec", "eval", "getattr", "setattr", "compile", "input"):
        assert name not in b


# --- timeout -------------------------------------------------------------- #
def test_run_with_timeout_raises_on_infinite_loop():
    def spin():
        x = 0
        while True:
            x += 1

    with pytest.raises(StrategyTimeout):
        run_with_timeout(spin, (), 0.3)


def test_run_with_timeout_returns_fast_result():
    assert run_with_timeout(lambda: 42, (), 1.0) == 42


def test_timeout_strategy_is_caught_by_wrapper():
    from evolver.config import Config
    from evolver.strategy import LoadedStrategy

    cfg = Config()
    strat = LoadedStrategy.create(strategy_source("slow", BODY_TIMEOUT), 1, cfg)
    # Three timeouts -> auto-retired.
    for _ in range(cfg.max_failures):
        assert strat.decide(_FakeCtx(), 0.2, cfg.max_failures) is None
    assert strat.retired
    assert "auto-retired" in strat.retired_reason


class _FakeCtx:
    candles = [{"time": 1, "open": 1.0, "close": 1.0}, {"time": 2, "open": 1.0, "close": 2.0}]
    window_open_price = 1.0
    seconds_remaining = 300
    spot = 2.0
    books = {"Up": {"asks": [(0.5, 10)], "bids": []}, "Down": {"asks": [(0.5, 10)], "bids": []}}

    @staticmethod
    def fee(shares, price):
        return 0.0

    @staticmethod
    def breakeven(ask):
        return ask
