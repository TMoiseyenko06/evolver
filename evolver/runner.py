"""The eternal loop: seed → forward-test → rank → report → evolve → repeat.

Kept separate from :mod:`evolver.generation` (which holds the per-step logic) so
the loop's control flow reads top-to-bottom. The market provider and OpenRouter
client are injected, so this same loop runs live or fully mocked in tests.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .config import Config
from .generation import evolve, rank_and_cull, run_generation, seed_population
from .market import MarketProvider
from .reporting import write_generation_report
from .store import Store
from .strategy import LoadedStrategy

log = logging.getLogger("evolver")


def _persist_generation_state(store: Store, population, survivors, generation) -> None:
    survivor_names = {s.name for s in survivors}
    ranked = sorted(population, key=lambda s: (s.gen.net_pnl, s.gen.tiebreak), reverse=True)
    for rank, s in enumerate(ranked, 1):
        alive = s.name in survivor_names
        store.save_state(s, alive=alive, generation=generation)
        store.save_gen_stats(generation, s, survived=alive, rank=rank)


def run_one_generation(
    population: List[LoadedStrategy],
    market: MarketProvider,
    client,
    store: Store,
    config: Config,
    generation: int,
) -> List[LoadedStrategy]:
    """Run generation ``generation`` and return the next generation's population."""
    log.info("=== generation %d: forward-testing %d strategies over %d windows ===",
             generation, len(population), config.windows_per_generation)
    run_generation(population, market, store, config, generation)

    survivors, retirees = rank_and_cull(population, config)
    report_path = write_generation_report(config, generation, population, survivors)
    _persist_generation_state(store, population, survivors, generation)
    store.finish_generation(generation, report_path)
    log.info("generation %d survivors: %s", generation, [s.name for s in survivors])

    replacements = evolve(survivors, retirees, client, store, config, generation + 1)
    next_population = survivors + replacements
    for s in next_population:
        store.save_state(s, alive=True, generation=generation + 1)
    log.info("generation %d complete; next population size %d", generation, len(next_population))
    return next_population


def run_loop(
    market: MarketProvider,
    client,
    store: Store,
    config: Config,
    max_generations: Optional[int] = None,
) -> None:
    """Seed (or resume) and loop forever (or for ``max_generations`` in tests)."""
    population = store.load_alive_strategies(config)

    if not population:
        generation = 1
        log.info("seeding generation 1 with %d strategies", config.population_size)
        population = seed_population(client, store, config)
        store.start_generation(1)
    else:
        # Resume: continue at the next generation that still needs to run.
        generation = store.next_generation_to_run()
        log.info("resuming with %d alive strategies at generation %d", len(population), generation)

    completed = 0
    while True:
        population = run_one_generation(population, market, client, store, config, generation)
        generation += 1
        completed += 1
        if max_generations is not None and completed >= max_generations:
            log.info("reached max_generations=%d; stopping loop", max_generations)
            return
