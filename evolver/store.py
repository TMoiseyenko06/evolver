"""SQLite + filesystem persistence — everything repeatable.

Layout (all under ``config.data_dir``):
  * ``evolver.sqlite``          — the database (schema below).
  * ``strategies/gen{G}_{name}.py`` — every strategy's exact source.
  * ``runs/gen{G}_report.md``   — per-generation leaderboards.

The database records, for every strategy: source, source hash, lineage, model,
timestamp, and the exact prompt+response that produced it. For every window:
the candle-state hash, full poll snapshots (books included), every decision
(passes too), fills, and outcomes — enough to ``replay`` deterministically.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from polybot import db

from .config import Config
from .models import Decision, Stats, TradeResult, WindowData
from .strategy import LoadedStrategy

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER,
    kind TEXT,
    model TEXT,
    system TEXT,
    user TEXT,
    response TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS strategies (
    name TEXT PRIMARY KEY,
    description TEXT,
    source TEXT,
    source_path TEXT,
    source_hash TEXT,
    generation_created INTEGER,
    lineage_json TEXT,
    model TEXT,
    prompt_id INTEGER,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS strategy_state (
    name TEXT PRIMARY KEY,
    alive INTEGER,
    bankroll REAL,
    generations_survived INTEGER,
    lifetime_json TEXT,
    retired_reason TEXT,
    last_generation INTEGER
);

CREATE TABLE IF NOT EXISTS generations (
    generation INTEGER PRIMARY KEY,
    started_at REAL,
    ended_at REAL,
    report_path TEXT
);

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER,
    seq INTEGER,
    window_id TEXT,
    condition_id TEXT,
    title TEXT,
    start_iso TEXT,
    end_iso TEXT,
    candle_state_hash TEXT,
    coinbase_side TEXT,
    official_side TEXT,
    resolved_side TEXT,
    mismatch INTEGER,
    data_json TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_row_id INTEGER,
    strategy_name TEXT,
    poll_index INTEGER,
    action_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_row_id INTEGER,
    generation INTEGER,
    strategy_name TEXT,
    side TEXT,
    shares REAL,
    cost REAL,
    avg_price REAL,
    fee REAL,
    won INTEGER,
    payout REAL,
    net_pnl REAL,
    breakeven REAL
);

CREATE TABLE IF NOT EXISTS gen_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER,
    strategy_name TEXT,
    trades INTEGER,
    wins INTEGER,
    net_pnl REAL,
    sum_breakeven REAL,
    fees_paid REAL,
    bankroll_end REAL,
    lifetime_net_pnl REAL,
    generations_survived INTEGER,
    survived INTEGER,
    rank INTEGER
);

CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER,
    window_id TEXT,
    driver TEXT,
    side TEXT,
    resolved_side TEXT,
    paper_price REAL,
    paper_shares REAL,
    paper_fee REAL,
    paper_cost REAL,
    paper_net_pnl REAL,
    paper_won INTEGER,
    real_price REAL,
    real_shares REAL,
    real_fee REAL,
    real_cost REAL,
    real_net_pnl REAL,
    real_won INTEGER,
    order_id TEXT,
    order_status TEXT,
    raw_json TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_windows_gen ON windows(generation, seq);
CREATE INDEX IF NOT EXISTS idx_decisions_win ON decisions(window_row_id);
CREATE INDEX IF NOT EXISTS idx_trades_strat ON trades(strategy_name);
CREATE INDEX IF NOT EXISTS idx_genstats_strat ON gen_stats(strategy_name);
"""


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.ensure_dirs()
        self.conn = db.connect(config.db_path)
        db.apply_schema(self.conn, SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- prompts / strategies -------------------------------------------- #
    def save_prompt(self, generation: int, kind: str, system: str, user: str, response: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO prompts (generation, kind, model, system, user, response, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (generation, kind, self.config.model, system, user, response, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def save_strategy(self, strat: LoadedStrategy, prompt_id: Optional[int]) -> None:
        """Persist a strategy's source file and its DB record (idempotent)."""
        fname = f"gen{strat.generation_created}_{strat.name}.py"
        path = self.config.strategies_dir / fname
        path.write_text(strat.source, encoding="utf-8")
        self.conn.execute(
            "INSERT OR REPLACE INTO strategies "
            "(name, description, source, source_path, source_hash, generation_created,"
            " lineage_json, model, prompt_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                strat.name,
                strat.description,
                strat.source,
                str(path),
                strat.source_hash,
                strat.generation_created,
                json.dumps(strat.lineage),
                self.config.model,
                prompt_id,
                time.time(),
            ),
        )
        self.conn.commit()

    def strategy_name_exists(self, name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM strategies WHERE name=?", (name,)
        ).fetchone() is not None

    # --- population state ------------------------------------------------- #
    def save_state(self, strat: LoadedStrategy, alive: bool, generation: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO strategy_state "
            "(name, alive, bankroll, generations_survived, lifetime_json, retired_reason, last_generation)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                strat.name,
                1 if alive else 0,
                strat.bankroll,
                strat.generations_survived,
                json.dumps(strat.lifetime.to_json()),
                strat.retired_reason,
                generation,
            ),
        )
        self.conn.commit()

    def load_alive_strategies(self, config: Config) -> List[LoadedStrategy]:
        """Reconstruct the currently-alive population for resuming a run."""
        rows = self.conn.execute(
            "SELECT s.*, st.bankroll, st.generations_survived, st.lifetime_json, st.retired_reason"
            " FROM strategy_state st JOIN strategies s ON s.name = st.name"
            " WHERE st.alive = 1"
        ).fetchall()
        out: List[LoadedStrategy] = []
        for r in rows:
            strat = LoadedStrategy(
                name=r["name"],
                description=r["description"],
                source=r["source"],
                generation_created=r["generation_created"],
                lineage=json.loads(r["lineage_json"] or "[]"),
                source_hash=r["source_hash"],
                bankroll=r["bankroll"],
                generations_survived=r["generations_survived"],
                lifetime=Stats.from_json(json.loads(r["lifetime_json"] or "{}")),
            )
            strat.bind(config)
            out.append(strat)
        return out

    # --- generations ------------------------------------------------------ #
    def start_generation(self, generation: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO generations (generation, started_at) VALUES (?,?)",
            (generation, time.time()),
        )
        self.conn.commit()

    def finish_generation(self, generation: int, report_path: str) -> None:
        self.conn.execute(
            "UPDATE generations SET ended_at=?, report_path=? WHERE generation=?",
            (time.time(), report_path, generation),
        )
        self.conn.commit()

    def current_generation(self) -> int:
        row = self.conn.execute("SELECT MAX(generation) AS g FROM generations").fetchone()
        return int(row["g"]) if row and row["g"] is not None else 0

    def next_generation_to_run(self) -> int:
        """The generation the loop should (re)start on when resuming.

        Prefer an unfinished (started-but-not-ended) generation; otherwise one
        past the last finished generation; otherwise generation 1.
        """
        row = self.conn.execute(
            "SELECT generation FROM generations WHERE ended_at IS NULL ORDER BY generation ASC LIMIT 1"
        ).fetchone()
        if row is not None:
            return int(row["generation"])
        row = self.conn.execute(
            "SELECT MAX(generation) AS g FROM generations WHERE ended_at IS NOT NULL"
        ).fetchone()
        return int(row["g"]) + 1 if row and row["g"] is not None else 1

    # --- windows / decisions / trades ------------------------------------ #
    def save_window(
        self,
        generation: int,
        seq: int,
        window: WindowData,
        decisions: List[Decision],
        trades: List[TradeResult],
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO windows (generation, seq, window_id, condition_id, title,"
            " start_iso, end_iso, candle_state_hash, coinbase_side, official_side,"
            " resolved_side, mismatch, data_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                generation,
                seq,
                window.window_id,
                window.condition_id,
                window.title,
                window.start_iso,
                window.end_iso,
                window.candle_state_hash,
                window.coinbase_side,
                window.official_side,
                window.resolved_side,
                1 if window.mismatch else 0,
                json.dumps(window.to_json()),
                time.time(),
            ),
        )
        win_id = cur.lastrowid
        for d in decisions:
            self.conn.execute(
                "INSERT INTO decisions (window_row_id, strategy_name, poll_index, action_json, error)"
                " VALUES (?,?,?,?,?)",
                (win_id, d.strategy_name, d.poll_index, json.dumps(d.action) if d.action else None, d.error),
            )
        for t in trades:
            self.conn.execute(
                "INSERT INTO trades (window_row_id, generation, strategy_name, side, shares,"
                " cost, avg_price, fee, won, payout, net_pnl, breakeven)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    win_id, generation, t.strategy_name, t.fill.side, t.fill.shares,
                    t.fill.cost, t.fill.avg_price, t.fill.fee, 1 if t.won else 0,
                    t.payout, t.net_pnl, t.breakeven,
                ),
            )
        self.conn.commit()
        return win_id

    def save_gen_stats(self, generation: int, strat: LoadedStrategy, survived: bool, rank: int) -> None:
        g = strat.gen
        self.conn.execute(
            "INSERT INTO gen_stats (generation, strategy_name, trades, wins, net_pnl,"
            " sum_breakeven, fees_paid, bankroll_end, lifetime_net_pnl, generations_survived,"
            " survived, rank) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                generation, strat.name, g.trades, g.wins, g.net_pnl, g.sum_breakeven,
                g.fees_paid, strat.bankroll, strat.lifetime.net_pnl,
                strat.generations_survived, 1 if survived else 0, rank,
            ),
        )
        self.conn.commit()

    # --- queries for CLI + diversity + replay ---------------------------- #
    def recent_windows(self, n: int) -> List[WindowData]:
        rows = self.conn.execute(
            "SELECT data_json FROM windows ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        windows = [WindowData.from_json(json.loads(r["data_json"])) for r in rows]
        windows.reverse()  # chronological
        return windows

    def windows_for_generations(self, generations: List[int]) -> List[WindowData]:
        if not generations:
            return []
        placeholders = ",".join("?" for _ in generations)
        rows = self.conn.execute(
            f"SELECT data_json FROM windows WHERE generation IN ({placeholders}) ORDER BY id ASC",
            tuple(generations),
        ).fetchall()
        return [WindowData.from_json(json.loads(r["data_json"])) for r in rows]

    def generations_for_strategy(self, name: str) -> List[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT generation FROM gen_stats WHERE strategy_name=? ORDER BY generation",
            (name,),
        ).fetchall()
        return [int(r["generation"]) for r in rows]

    def get_strategy_row(self, name: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM strategies WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def get_state_row(self, name: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM strategy_state WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def stat_history(self, name: str) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM gen_stats WHERE strategy_name=? ORDER BY generation", (name,)
        ).fetchall()
        return [dict(r) for r in rows]

    def leaderboard_rows(self) -> List[dict]:
        """Lifetime rankings across all strategies ever created."""
        rows = self.conn.execute(
            "SELECT s.name, s.lineage_json, s.generation_created, st.alive, st.bankroll,"
            " st.generations_survived, st.lifetime_json, st.retired_reason"
            " FROM strategies s LEFT JOIN strategy_state st ON s.name = st.name"
        ).fetchall()
        out = []
        for r in rows:
            lt = Stats.from_json(json.loads(r["lifetime_json"] or "{}"))
            out.append(
                {
                    "name": r["name"],
                    "lineage": json.loads(r["lineage_json"] or "[]"),
                    "generation_created": r["generation_created"],
                    "alive": bool(r["alive"]),
                    "bankroll": r["bankroll"] if r["bankroll"] is not None else self.config.starting_bankroll,
                    "generations_survived": r["generations_survived"] or 0,
                    "retired_reason": r["retired_reason"],
                    "stats": lt,
                }
            )
        out.sort(key=lambda d: (d["stats"].net_pnl, d["stats"].tiebreak), reverse=True)
        return out

    def all_strategy_sources(self) -> Dict[str, str]:
        rows = self.conn.execute("SELECT name, source FROM strategies").fetchall()
        return {r["name"]: r["source"] for r in rows}

    # --- calibration (paper-vs-real experiment) --------------------------- #
    def save_calibration(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT INTO calibration (seq, window_id, driver, side, resolved_side,"
            " paper_price, paper_shares, paper_fee, paper_cost, paper_net_pnl, paper_won,"
            " real_price, real_shares, real_fee, real_cost, real_net_pnl, real_won,"
            " order_id, order_status, raw_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec["seq"], rec["window_id"], rec["driver"], rec["side"], rec["resolved_side"],
                rec["paper_price"], rec["paper_shares"], rec["paper_fee"], rec["paper_cost"],
                rec["paper_net_pnl"], 1 if rec["paper_won"] else 0,
                rec["real_price"], rec["real_shares"], rec["real_fee"], rec["real_cost"],
                rec["real_net_pnl"], 1 if rec["real_won"] else 0,
                rec.get("order_id"), rec.get("order_status"), rec.get("raw_json"), time.time(),
            ),
        )
        self.conn.commit()

    def calibration_rows(self) -> List[dict]:
        rows = self.conn.execute("SELECT * FROM calibration ORDER BY id").fetchall()
        return [dict(r) for r in rows]
