"""Parameter fine-tuning: search ONE strategy's numeric parameters.

Unlike the main evolver (which searches over LLM-written *structures*), this keeps
a strategy's logic fixed and searches over its numeric *parameters* — a continuous
space where survivors' values carry real information generation to generation, so
it converges far faster on small (50-window) samples.

Flow:
  1. Get a parameterized TEMPLATE of the strategy: source with ``{{param}}``
     placeholders + a spec of ranges. Either the LLM extracts it from a strategy
     (``parameterize``), or you supply a template + ranges manually.
  2. Population = N variants, each the template rendered with one parameter set.
     Gen 1 samples the space; gen 2+ keeps the top K and breeds the rest by nudging
     survivors' params (+ a little random exploration).
  3. Forward-test all N over the same 50 windows via the existing engine, rank,
     mutate, repeat.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import Config
from .generation import rank_and_cull, run_generation
from .market import MarketProvider
from .openrouter import extract_code_blocks
from .sandbox import SandboxError
from .strategy import LoadedStrategy
from .store import Store

# --------------------------------------------------------------------------- #
# Parameter space
# --------------------------------------------------------------------------- #
@dataclass
class ParamSpec:
    name: str
    lo: float
    hi: float
    is_int: bool = False
    default: Optional[float] = None


def parse_param_spec(spec: Dict[str, dict]) -> List[ParamSpec]:
    """Parse ``{name: {min, max, type, default}}`` into ParamSpec list."""
    out: List[ParamSpec] = []
    for name, cfg in spec.items():
        out.append(ParamSpec(
            name=str(name),
            lo=float(cfg["min"]),
            hi=float(cfg["max"]),
            is_int=str(cfg.get("type", "float")).lower().startswith("int"),
            default=cfg.get("default"),
        ))
    if not out:
        raise ValueError("empty parameter spec")
    return out


def _coerce(spec: ParamSpec, value: float) -> float:
    value = max(spec.lo, min(spec.hi, value))
    return int(round(value)) if spec.is_int else value


def sample_params(specs: List[ParamSpec], rng: random.Random) -> Dict[str, float]:
    return {s.name: _coerce(s, rng.uniform(s.lo, s.hi)) for s in specs}


def mutate_params(base: Dict[str, float], specs: List[ParamSpec],
                  rng: random.Random, sigma: float = 0.2) -> Dict[str, float]:
    """Gaussian nudge each param by ``sigma`` of its range, clamped to bounds."""
    out: Dict[str, float] = {}
    for s in specs:
        cur = float(base.get(s.name, (s.lo + s.hi) / 2))
        out[s.name] = _coerce(s, cur + rng.gauss(0.0, sigma * (s.hi - s.lo)))
    return out


def _fmt(value) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def render_template(template: str, params: Dict[str, float]) -> str:
    src = template
    for name, value in params.items():
        src = src.replace("{{" + name + "}}", _fmt(value))
    return src


# --------------------------------------------------------------------------- #
# LLM parameterization (optional setup step)
# --------------------------------------------------------------------------- #
PARAM_SYSTEM = """You convert a trading strategy into a tunable TEMPLATE for \
parameter optimization. Identify the numeric CONSTANTS that are meaningful tunable \
knobs (entry-timing thresholds on seconds_remaining, price caps, edge margins, \
lookback lengths, multipliers, volume ratios). Do NOT parameterize structural \
integers like list indices.

Return TWO fenced blocks:
1) A ```python block: the SAME strategy, byte-for-byte identical EXCEPT each chosen \
constant replaced by a {{placeholder}} (snake_case name). Keep `class Strategy` with \
NAME/DESCRIPTION/decide, keep imports to only math/statistics, keep all logic.
2) A ```json block: an object mapping each placeholder name to \
{"min": <low>, "max": <high>, "type": "int"|"float", "default": <original value>}. \
Choose sensible ranges bracketing the original value. Use 3-8 parameters."""


_PY_BLOCK = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def parameterize(client, source: str) -> Tuple[str, List[ParamSpec]]:
    """One LLM call: turn a strategy into (template, param specs)."""
    resp = client.chat(PARAM_SYSTEM, f"Parameterize this strategy:\n\n```python\n{source}\n```")
    py = _PY_BLOCK.findall(resp)
    js = _JSON_BLOCK.findall(resp)
    if not py or not js:
        raise ValueError("model did not return a python template AND a json spec")
    template = py[0].strip()
    specs = parse_param_spec(json.loads(js[0]))
    # Sanity: every placeholder in the template must have a spec.
    placeholders = set(re.findall(r"\{\{(\w+)\}\}", template))
    missing = placeholders - {s.name for s in specs}
    if missing:
        raise ValueError(f"template placeholders without a spec: {missing}")
    return template, specs


# --------------------------------------------------------------------------- #
# Variant construction + tuning loop
# --------------------------------------------------------------------------- #
def _unique(name: str, taken: set) -> str:
    if name not in taken:
        return name
    i = 2
    while f"{name}_{i}" in taken:
        i += 1
    return f"{name}_{i}"


def build_variant(template: str, params: Dict[str, float], base_label: str,
                  generation: int, index: int, config: Config, taken: set) -> Optional[LoadedStrategy]:
    """Render params into source and compile it. Returns None if it won't build."""
    source = render_template(template, params)
    if "{{" in source:
        return None  # unfilled placeholder
    name = _unique(f"{base_label}_g{generation}_v{index}", taken)
    try:
        strat = LoadedStrategy.create(
            source, generation, config, lineage=[base_label], name=name,
            description=f"tuned variant of {base_label}: {params}",
        )
    except SandboxError:
        return None
    strat.params = dict(params)  # carried for mutation/reporting
    taken.add(name)
    return strat


def _new_variants(template, specs, base_label, generation, config, taken, count,
                  rng, parents=None, explore=0.15):
    """Make ``count`` variants: mutate parents (if any) with some fresh sampling."""
    out: List[LoadedStrategy] = []
    attempts = 0
    while len(out) < count and attempts < count * 20:
        attempts += 1
        if parents and rng.random() >= explore:
            params = mutate_params(rng.choice(parents).params, specs, rng)
        else:
            params = sample_params(specs, rng)
        strat = build_variant(template, params, base_label, generation, len(out), config, taken)
        if strat is not None:
            out.append(strat)
    return out


def run_tuning(
    base_label: str,
    template: str,
    specs: List[ParamSpec],
    market: MarketProvider,
    store: Store,
    config: Config,
    variants: int,
    keep: int,
    generations: Optional[int] = None,
    rng: Optional[random.Random] = None,
    on_generation=None,
) -> List[LoadedStrategy]:
    """Evolutionary parameter search. Returns the final population."""
    rng = rng or random.Random()
    config.population_size = variants
    config.survivors = keep
    taken: set = set(store.all_strategy_sources().keys())

    population = _new_variants(template, specs, base_label, 1, config, taken, variants, rng)
    for s in population:
        store.save_strategy(s, None)
        store.save_state(s, alive=True, generation=1)

    generation = 1
    completed = 0
    while population:
        run_generation(population, market, store, config, generation)
        survivors, retirees = rank_and_cull(population, config)
        _persist(store, population, survivors, generation)
        report = write_tune_report(config, base_label, generation, population, survivors)
        store.finish_generation(generation, report)
        if on_generation is not None:
            on_generation(generation, population, survivors)

        completed += 1
        if generations is not None and completed >= generations:
            return population

        children = _new_variants(template, specs, base_label, generation + 1, config,
                                 taken, variants - len(survivors), rng, parents=survivors)
        for s in children:
            store.save_strategy(s, None)
        population = survivors + children
        for s in population:
            store.save_state(s, alive=True, generation=generation + 1)
        generation += 1
    return population


def _persist(store, population, survivors, generation):
    survivor_names = {s.name for s in survivors}
    ranked = sorted(population, key=lambda s: (s.gen.net_pnl, s.gen.tiebreak), reverse=True)
    for rank, s in enumerate(ranked, 1):
        alive = s.name in survivor_names
        store.save_state(s, alive=alive, generation=generation)
        store.save_gen_stats(generation, s, survived=alive, rank=rank)


def write_tune_report(config: Config, base_label: str, generation: int,
                      population: List[LoadedStrategy], survivors) -> str:
    survivor_names = {s.name for s in survivors}
    ranked = sorted(population, key=lambda s: (s.gen.net_pnl, s.gen.tiebreak), reverse=True)
    best = ranked[0] if ranked else None
    lines = [f"# Tuning {base_label} — generation {generation}\n"]
    if best is not None:
        lines.append(f"**Best this gen:** net P&L ${best.gen.net_pnl:+.2f} "
                     f"(hit {best.gen.hit_pct*100:.1f}%, {best.gen.trades} trades)")
        lines.append(f"**Best params:** `{getattr(best, 'params', {})}`\n")
    lines.append("| Rank | Variant | Net P&L | Hit% | Trades | Kept | Params |")
    lines.append("|---:|---|---:|---:|---:|:---:|---|")
    for i, s in enumerate(ranked, 1):
        g = s.gen
        kept = "✓" if s.name in survivor_names else ""
        lines.append(f"| {i} | {s.name} | ${g.net_pnl:+.2f} | {g.hit_pct*100:.1f} "
                     f"| {g.trades} | {kept} | `{getattr(s, 'params', {})}` |")
    report = "\n".join(lines) + "\n"
    path = config.runs_dir / f"tune_{base_label}_gen{generation}.md"
    path.write_text(report, encoding="utf-8")
    return str(path)
