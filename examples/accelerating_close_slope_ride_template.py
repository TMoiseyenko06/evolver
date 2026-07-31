# lineage: momentum_slope_persistence, three_bar_impulse_ride
import statistics

class Strategy:
    NAME = "accelerating_close_slope_ride"
    DESCRIPTION = (
        "Accelerating close-slope ride: fits the slope of the last 3 closes and the last 5 "
        "closes; fires only when the SHORT slope is steeper than the LONG slope in the same "
        "direction (momentum accelerating) and agrees with drift. Acceleration into resolution "
        "means the leg is gaining, not fading, so a reversal in the remaining time is unlikely. "
        "Distinct from the linear-slope survivor which only checks slope magnitude, not its "
        "second derivative. Cheap ask, mid window."
    )

    def decide(self, ctx):
        c = ctx.candles
        if len(c) < 6 or ctx.seconds_remaining < 80 or ctx.seconds_remaining > 220:
            return None

        def slope(vals):
            n = len(vals)
            xs = list(range(n))
            mx = statistics.mean(xs)
            my = statistics.mean(vals)
            den = sum((x - mx) ** 2 for x in xs)
            if den <= 0:
                return None
            return sum((xs[i] - mx) * (vals[i] - my) for i in range(n)) / den

        short = slope([x["close"] for x in c[-3:]])
        lng = slope([x["close"] for x in c[-5:]])
        if short is None or lng is None:
            return None
        if short * lng <= 0:
            return None
        if abs(short) <= abs(lng):
            return None
        typ = statistics.mean([x["high"] - x["low"] for x in c[-5:]])
        if typ <= 0 or abs(short) < typ * {{accel_ratio}}:
            return None
        op = ctx.window_open_price
        if op <= 0:
            return None
        drift = ctx.spot - op
        if short * drift <= 0:
            return None
        side = "Up" if short > 0 else "Down"
        asks = ctx.books.get(side, {}).get("asks", [])
        if not asks:
            return None
        ask = asks[0][0]
        if ask > {{max_ask}}:
            return None
        est_p = 0.5 + min(0.17, abs(short) / max(typ, 1e-9) * 0.10 + 0.02)
        if est_p - ctx.breakeven(ask) > {{min_edge}}:
            return {"side": side}
        return None
