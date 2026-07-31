# lineage: novel
import statistics

class Strategy:
    NAME = "range_compression_pin_cheap"
    DESCRIPTION = (
        "Range-compression pin: when the last several bars are unusually NARROW (range "
        "well below baseline) AND volume is quiet, volatility is dead and the window is "
        "likely to pin very close to current spot. The side spot already favors (sign "
        "of tiny drift from open) is near-certain to hold with no fuel to move. We buy "
        "that side only when its ask is cheap so the near-flat fee lets a modest "
        "edge survive. Keys off RANGE contraction, not price direction."
    )

    def decide(self, ctx):
        c = ctx.candles
        if len(c) < 8 or ctx.seconds_remaining < 50 or ctx.seconds_remaining > 150:
            return None
        ranges = [x["high"] - x["low"] for x in c]
        base_rng = statistics.mean(ranges[-8:-3])
        recent_rng = statistics.mean(ranges[-3:])
        if base_rng <= 0 or recent_rng > base_rng * {{range_ratio}}:
            return None
        vols = [x["volume"] for x in c]
        base_v = statistics.mean(vols[-8:-3])
        recent_v = statistics.mean(vols[-3:])
        if base_v <= 0 or recent_v > base_v * {{vol_ratio}}:
            return None
        drift = ctx.spot - ctx.window_open_price
        if abs(drift) < 1e-9:
            return None
        side = "Up" if drift > 0 else "Down"
        asks = ctx.books.get(side, {}).get("asks", [])
        if not asks:
            return None
        ask = asks[0][0]
        if ask > {{max_ask}}:
            return None
        est_p = 0.60
        if est_p - ctx.breakeven(ask) > {{min_edge}}:
            return {"side": side}
        return None
