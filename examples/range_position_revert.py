# lineage: novel
import math
import statistics

class Strategy:
    NAME = "range_position_revert"
    DESCRIPTION = (
        "Intrabar range-position reversion: using recent candle highs/lows we estimate where spot "
        "sits within its recent range. When spot is stretched to an extreme of the range AND the "
        "corresponding continuation side is cheaply fadeable, we buy the reversion side. Uses low "
        "prices to keep fees negligible. Mid-window only."
    )

    def decide(self, ctx):
        if ctx.seconds_remaining > 170 or ctx.seconds_remaining < 45:
            return None
        c = ctx.candles
        if len(c) < 6:
            return None
        hi = max(x["high"] for x in c[-6:])
        lo = min(x["low"] for x in c[-6:])
        rng = hi - lo
        if rng <= 0:
            return None
        pos = (ctx.spot - lo) / rng  # 0 bottom, 1 top
        if pos > 0.90:
            # overextended up -> fade with Down
            side = "Down"
        elif pos < 0.10:
            side = "Up"
        else:
            return None
        book = ctx.books.get(side)
        if not book or not book.get("asks"):
            return None
        ask = book["asks"][0][0]
        if ask > 0.42:
            return None
        est_p = ask + 0.11
        if est_p - ctx.breakeven(ask) > 0.03:
            return {"side": side}
        return None
