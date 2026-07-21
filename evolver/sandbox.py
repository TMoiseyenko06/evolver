"""AST validation + restricted execution for untrusted LLM-generated strategy code.

The threat model: the model returns Python we exec. Before exec we walk the AST
and reject anything dangerous; at exec time we swap in a minimal ``__builtins__``
and an ``__import__`` that only admits an allowlist of modules.

Rejection rules (validation, before any exec):
  * ``import``/``from ... import`` of any module outside the allowlist
    (default ``{math, statistics}``).
  * any attribute access to a dunder (``x.__class__``, ``().__globals__`` …).
  * any reference to a dangerous name: exec, eval, open, compile, __import__,
    globals, locals, vars, getattr/setattr/delattr, input, os, sys, requests,
    socket, subprocess, and friends.

Defense in depth: even if validation is somehow bypassed, the exec namespace has
no ``open``/``exec``/``eval``/``__import__`` except the guarded importer, so a
strategy cannot reach the filesystem, network, or process.
"""

from __future__ import annotations

import ast
import signal
from typing import Any, Callable, FrozenSet, Tuple

DEFAULT_ALLOWED_IMPORTS: FrozenSet[str] = frozenset({"math", "statistics"})

# Names that must never appear anywhere in strategy source.
FORBIDDEN_NAMES: FrozenSet[str] = frozenset(
    {
        "exec", "eval", "compile", "open", "__import__", "input",
        "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
        "os", "sys", "subprocess", "socket", "requests", "urllib", "http",
        "importlib", "builtins", "breakpoint", "memoryview", "classmethod",
        "staticmethod", "super", "type", "object", "help", "exit", "quit",
    }
)


class SandboxError(Exception):
    """Raised when strategy source fails validation."""


class StrategyTimeout(Exception):
    """Raised when a strategy call exceeds its time budget."""


def validate(source: str, allowed_imports: FrozenSet[str] = DEFAULT_ALLOWED_IMPORTS) -> None:
    """Raise :class:`SandboxError` if ``source`` violates any sandbox rule."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SandboxError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        # --- imports ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed_imports:
                    raise SandboxError(f"import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in allowed_imports:
                raise SandboxError(f"import from '{node.module}' is not allowed")

        # --- dunder attribute access (x.__class__, etc.) ---
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise SandboxError(f"attribute access to dunder '{node.attr}' is not allowed")
            if node.attr in FORBIDDEN_NAMES:
                raise SandboxError(f"attribute access to '{node.attr}' is not allowed")

        # --- dangerous / dunder names ---
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise SandboxError(f"use of name '{node.id}' is not allowed")
            if node.id.startswith("__") and node.id.endswith("__"):
                raise SandboxError(f"use of dunder name '{node.id}' is not allowed")

        # --- keyword arg named like a dunder (rare, but block it) ---
        elif isinstance(node, ast.keyword) and node.arg and node.arg.startswith("__"):
            raise SandboxError(f"keyword '{node.arg}' is not allowed")


# A minimal, safe builtins set. No open/exec/eval/__import__/getattr/etc.
_SAFE_BUILTIN_NAMES = [
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "int", "len", "list", "map", "max", "min",
    "pow", "range", "reversed", "round", "set", "slice", "sorted", "str", "sum",
    "tuple", "zip", "True", "False", "None", "abs", "bytes", "hasattr",
]


def _make_safe_import(allowed_imports: FrozenSet[str]) -> Callable:
    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root not in allowed_imports:
            raise SandboxError(f"import of '{name}' blocked at runtime")
        return __import__(name, globals, locals, fromlist, level)

    return _safe_import


def _safe_builtins(allowed_imports: FrozenSet[str]) -> dict:
    import builtins as _b

    safe = {n: getattr(_b, n) for n in _SAFE_BUILTIN_NAMES if hasattr(_b, n)}
    # Expose a limited set of exception types so strategies can use try/except.
    for exc in ("Exception", "ValueError", "TypeError", "ZeroDivisionError",
                "IndexError", "KeyError", "ArithmeticError", "StopIteration"):
        safe[exc] = getattr(_b, exc)
    # `__build_class__` is the implicit machinery a `class` statement compiles to;
    # it is not reachable from (validated) source but must exist to define the
    # Strategy class. `__import__` is the guarded importer above.
    safe["__build_class__"] = _b.__build_class__
    safe["__import__"] = _make_safe_import(allowed_imports)
    return safe


def compile_strategy(
    source: str, allowed_imports: FrozenSet[str] = DEFAULT_ALLOWED_IMPORTS
) -> Any:
    """Validate, exec in a restricted namespace, and return a ``Strategy()`` instance.

    Raises :class:`SandboxError` if validation fails or the module does not define
    a usable ``Strategy`` class.
    """
    validate(source, allowed_imports)
    # `__name__` is required by class-definition machinery (sets `__module__`).
    namespace: dict = {"__builtins__": _safe_builtins(allowed_imports), "__name__": "strategy"}
    try:
        code = compile(source, "<strategy>", "exec")
        exec(code, namespace)  # noqa: S102 — sandboxed namespace, validated AST
    except SandboxError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SandboxError(f"error executing strategy module: {exc!r}") from exc

    cls = namespace.get("Strategy")
    if cls is None:
        raise SandboxError("no 'Strategy' class defined")
    if not hasattr(cls, "decide"):
        raise SandboxError("Strategy class has no 'decide' method")
    try:
        instance = cls()
    except Exception as exc:  # noqa: BLE001
        raise SandboxError(f"error instantiating Strategy: {exc!r}") from exc
    return instance


# --------------------------------------------------------------------------- #
# Timeout runner
# --------------------------------------------------------------------------- #
def run_with_timeout(func: Callable, args: Tuple, timeout: float) -> Any:
    """Run ``func(*args)`` with a hard wall-clock ``timeout`` (seconds).

    Uses ``SIGALRM`` when available (main thread on POSIX), which interrupts even
    a pure-Python infinite loop between bytecodes. Falls back to running without a
    hard limit if signals are unavailable (e.g. off the main thread); callers
    still catch exceptions and, in the fallback, elapsed-time is measured so an
    over-budget call is still counted as a timeout.
    """
    if hasattr(signal, "SIGALRM"):
        try:
            return _run_with_sigalrm(func, args, timeout)
        except ValueError:
            # "signal only works in main thread" — fall through to soft timing.
            pass
    return _run_soft(func, args, timeout)


def _run_with_sigalrm(func: Callable, args: Tuple, timeout: float) -> Any:
    def _handler(signum, frame):
        raise StrategyTimeout(f"decide() exceeded {timeout}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return func(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _run_soft(func: Callable, args: Tuple, timeout: float) -> Any:
    import time as _t

    start = _t.monotonic()
    result = func(*args)
    if _t.monotonic() - start > timeout:
        raise StrategyTimeout(f"decide() exceeded {timeout}s (soft)")
    return result
