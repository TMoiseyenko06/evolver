"""Synthesis order-placement client (real orders on Polymarket via synthesis.trade).

Only what the calibration experiment needs: place a MARKET order and read it back.
Endpoints (from the Synthesis API reference):

- ``POST /api/v1/wallet/pol/{wallet_id}/order`` — create order.
  Body: ``token_id`` (numeric string), ``side`` BUY/SELL, ``type`` MARKET/LIMIT/
  STOPLOSS, ``amount`` (string), ``units`` USDC/SHARES, optional ``price`` (for
  MARKET a slippage cap, 0<p<=1).
  Response: ``order_id``, ``shares``, ``filled``, ``price`` (execution price),
  ``fee`` (object), ``status`` (e.g. MATCHED), timestamps.
- ``GET /api/v1/wallet/pol/{wallet_id}/order/{order_id}`` — read an order back.
- ``GET /api/v1/wallet/pol/{wallet_id}/balance`` — wallet balance (best-effort preflight).

Auth: ``X-API-KEY`` header. This module is intentionally thin and never retries a
create-order automatically — a failed placement is surfaced, never silently
re-sent, so we can't accidentally double-fill real money.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from . import polymarket as pm


class SynthesisError(RuntimeError):
    pass


@dataclass
class OrderResult:
    order_id: str
    token_id: str
    side: str
    type: str
    status: str
    amount_usdc: float   # USDC committed
    filled: float        # USDC actually filled
    shares: float        # shares received
    price: float         # volume-weighted execution price
    fee: float           # total fee in USDC
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.status.upper() in ("MATCHED", "FILLED", "COMPLETE", "COMPLETED")


@dataclass
class SynthesisClient:
    api_key: str
    wallet_id: str
    base_url: str = "https://synthesis.trade"
    timeout: float = 30.0

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:  # market-data endpoints are public; only send key when set
            headers["X-API-KEY"] = self.api_key
        return headers

    def _wallet_path(self, suffix: str = "") -> str:
        return f"{self.base_url}/api/v1/wallet/pol/{self.wallet_id}{suffix}"

    def place_market_order(
        self,
        token_id: str,
        side: str,
        usdc_amount: float,
        slippage_cap: Optional[float] = None,
    ) -> OrderResult:
        """Place a MARKET buy/sell of ``usdc_amount`` USDC of ``token_id``.

        ``slippage_cap`` (0<p<=1) is passed as the MARKET ``price`` guard so we
        never pay above it. Raises :class:`SynthesisError` on any non-2xx.
        """
        if not self.api_key or not self.wallet_id:
            raise SynthesisError("Synthesis api_key/wallet_id not configured")
        body: Dict[str, Any] = {
            "token_id": str(token_id),
            "side": side.upper(),
            "type": "MARKET",
            "amount": str(usdc_amount),
            "units": "USDC",
        }
        if slippage_cap is not None:
            body["price"] = str(slippage_cap)
        try:
            resp = requests.post(
                self._wallet_path("/order"), json=body, headers=self._headers(), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SynthesisError(f"order request failed: {exc}") from exc
        if resp.status_code == 404:
            raise SynthesisError(
                f"order rejected 404 from {self._wallet_path('/order')} — check "
                f"SYNTHESIS_BASE_URL (should be https://synthesis.trade): {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            resp_body = resp.text[:800]
            # Include the exact request we sent and any request-id/trace-id style
            # response headers — a generic body like "Failed to create order" gives
            # no clue on its own whether OUR request was malformed or the venue had
            # an internal issue; this makes the next occurrence self-diagnosing and
            # gives something concrete to hand to Synthesis support.
            trace_headers = {
                k: v for k, v in resp.headers.items()
                if any(t in k.lower() for t in ("request-id", "trace", "ray", "correlation"))
            }
            raise SynthesisError(
                f"order rejected {resp.status_code}: {resp_body} | request sent: {body} "
                f"| response headers: {trace_headers or dict(resp.headers)}"
            )
        return parse_order(resp.json())

    def get_order(self, order_id: str) -> Dict[str, Any]:
        resp = requests.get(
            self._wallet_path(f"/order/{order_id}"), headers=self._headers(), timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def get_balance(self) -> Optional[float]:
        """Best-effort USDC balance for display; None if unreadable."""
        try:
            resp = requests.get(self._wallet_path("/balance"), headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            return extract_usdc_balance(resp.json())
        except Exception:  # noqa: BLE001 — best-effort, non-fatal
            return None

    def balance_raw(self) -> str:
        """Raw balance body (truncated) for diagnosing a parse miss."""
        try:
            resp = requests.get(self._wallet_path("/balance"), headers=self._headers(), timeout=self.timeout)
            return f"HTTP {resp.status_code}: {resp.text[:400]}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"

    def check_reachable(self):
        """Preflight the wallet endpoint so a wrong host/path fails fast.

        Returns ``(ok, detail)``. ``ok`` is False on 404/401/403 or a connection
        error — i.e. before we bother waiting for a live trading window.
        """
        url = self._wallet_path("/balance")
        if not self.api_key or not self.wallet_id:
            return False, "SYNTHESIS_API_KEY / SYNTHESIS_WALLET_ID not set"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            return False, f"could not reach {url}: {exc}"
        if resp.status_code == 404:
            return False, (f"404 from {url} — check SYNTHESIS_BASE_URL "
                           f"(should be https://synthesis.trade, not the docs host)")
        if resp.status_code in (401, 403):
            return False, f"{resp.status_code} from {url} — check SYNTHESIS_API_KEY / wallet id"
        if resp.status_code >= 400:
            return False, f"{resp.status_code} from {url}: {resp.text[:200]}"
        return True, "ok"


def _unwrap(data: Any) -> Any:
    """Peel a ``{"success":..,"response":{..}}`` (or data/result) envelope."""
    seen = 0
    while isinstance(data, dict) and seen < 5:
        for key in ("response", "data", "result"):
            inner = data.get(key)
            if isinstance(inner, (dict, list)):
                data = inner
                break
        else:
            break
        seen += 1
    return data


def _pick(data: Dict[str, Any], keys, default=None):
    for k in keys:
        if isinstance(data, dict) and data.get(k) is not None:
            return data[k]
    return default


def parse_order(data: Dict[str, Any]) -> OrderResult:
    """Map a create-order response into an :class:`OrderResult`.

    Tolerant of Synthesis's response envelope and field-name variants; the full
    original response is preserved in ``raw`` for auditing.
    """
    body = _unwrap(data)
    body = body if isinstance(body, dict) else {}
    return OrderResult(
        order_id=str(_pick(body, ("order_id", "id", "orderID", "orderId"), "")),
        token_id=str(_pick(body, ("token_id", "tokenId"), "")),
        side=str(_pick(body, ("side",), "")),
        type=str(_pick(body, ("type", "order_type"), "")),
        status=str(_pick(body, ("status", "state"), "")),
        amount_usdc=_num(_pick(body, ("amount", "amount_usdc", "usdc"))),
        filled=_num(_pick(body, ("filled", "filled_amount", "matched_amount", "amount"))),
        shares=_num(_pick(body, ("shares", "size", "filled_size", "matched_size", "quantity"))),
        price=_num(_pick(body, ("price", "avg_price", "average_price", "fill_price"))),
        fee=parse_fee(_pick(body, ("fee", "fees"))),
        raw=data,
    )


def extract_usdc_balance(data: Any) -> Optional[float]:
    """Sum the USDC-family balance from a wallet-balance response, shape-agnostic.

    Recursively finds USDC amounts under the (unwrapped) body: a symbol->amount
    map (``{"USDC":"..","USDC.e":".."}``), a list of asset objects
    (``[{"symbol":"USDC","amount":".."}]``), or flat keys. Returns None when no
    USDC-like amount is found anywhere.
    """
    total, found = _sum_usdc(_unwrap(data))
    return total if found else None


_SYMBOL_KEYS = ("symbol", "token", "currency", "asset", "ticker")
_AMOUNT_KEYS = ("amount", "balance", "available", "value", "size", "quantity")


def _sum_usdc(node: Any, _depth: int = 0):
    """Return (sum, found) of USDC amounts anywhere within ``node``."""
    if _depth > 6:
        return 0.0, False
    total, found = 0.0, False
    if isinstance(node, dict):
        # symbol->amount map: key names the token.
        for k, v in node.items():
            if str(k).upper().startswith("USDC") and _is_num(v):
                total += _num(v)
                found = True
        # asset object: {symbol: USDC..., amount: ...}
        sym = _pick(node, _SYMBOL_KEYS)
        if sym is not None and str(sym).upper().startswith("USDC"):
            amt = _pick(node, _AMOUNT_KEYS)
            if _is_num(amt):
                total += _num(amt)
                found = True
        # recurse into nested containers.
        for v in node.values():
            if isinstance(v, (dict, list)):
                sub, sub_found = _sum_usdc(v, _depth + 1)
                total += sub
                found = found or sub_found
    elif isinstance(node, list):
        for item in node:
            sub, sub_found = _sum_usdc(item, _depth + 1)
            total += sub
            found = found or sub_found
    return total, found


def parse_fee(fee: Any) -> float:
    """Extract a total fee in USDC from the response's ``fee`` object/number."""
    if fee is None:
        return 0.0
    if isinstance(fee, (int, float, str)):
        return _num(fee)
    if isinstance(fee, dict):
        for key in ("amount", "total", "usdc", "value", "fee"):
            if key in fee:
                return _num(fee[key])
        # Fall back to summing numeric leaves.
        return sum(_num(v) for v in fee.values() if isinstance(v, (int, float, str)) and _is_num(v))
    return 0.0


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_num(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _first_present(data: Dict[str, Any], keys) -> Any:
    for k in keys:
        if isinstance(data, dict) and k in data:
            return data[k]
    return None


# --------------------------------------------------------------------------- #
# Market discovery + order books (Synthesis is the user's actual trading venue)
# --------------------------------------------------------------------------- #
# GET /api/v1/polymarket/markets  -> events with nested `markets`; per market:
#   condition_id, question, created_at, ends_at, active, resolved,
#   left_token_id/right_token_id, left_outcome/right_outcome, left_price/right_price
# POST /api/v1/markets/orderbooks -> body [token_id,...]; per token an `orderbook`
#   with bids/asks as {price_str: size_str} maps.

def _as_list(body: Any) -> List[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("events", "markets", "data", "results", "orderbooks"):
            if isinstance(body.get(k), list):
                return body[k]
        return [body]
    return []


def list_polymarket_markets(
    client: "SynthesisClient", title: str = "Bitcoin Up or Down", limit: int = 250,
    sort: str = "ends_at", order: str = "ASC",
) -> Any:
    """List markets by title (the `title` filter reliably surfaces the 5-min
    series; a broad `query` does not), soonest-ending first."""
    resp = requests.get(
        f"{client.base_url}/api/v1/polymarket/markets",
        params={"title": title, "limit": limit, "sort": sort, "order": order},
        headers=client._headers(), timeout=client.timeout,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_orderbook(client: "SynthesisClient", token_id: str) -> Any:
    resp = requests.post(
        f"{client.base_url}/api/v1/markets/orderbooks",
        json=[str(token_id)], headers=client._headers(), timeout=client.timeout,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_orderbooks(client: "SynthesisClient", token_ids: List[str]) -> Any:
    """Batch-fetch orderbooks for many token_ids at once (venue-agnostic endpoint)."""
    resp = requests.post(
        f"{client.base_url}/api/v1/markets/orderbooks",
        json=[str(t) for t in token_ids], headers=client._headers(), timeout=client.timeout,
    )
    resp.raise_for_status()
    return resp.json()


def list_markets(
    client: "SynthesisClient", venue: Optional[str] = None, limit: int = 250,
    offset: int = 0, sort: str = "volume", order: str = "DESC",
    min_ends_at: Optional[str] = None, max_ends_at: Optional[str] = None,
    tags: Optional[str] = None, live: Optional[bool] = None,
) -> Any:
    """GET /api/v1/markets — events (with nested markets) ACROSS venues.

    ``venue`` filters to ``polymarket`` or ``kalshi`` (None = both). This is the
    unified listing that lets us scan the entire market, not just one title.
    """
    params: Dict[str, Any] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
    if venue:
        params["venue"] = venue
    if min_ends_at:
        params["min_ends_at"] = min_ends_at
    if max_ends_at:
        params["max_ends_at"] = max_ends_at
    if tags:
        params["tags"] = tags
    if live is not None:
        params["live"] = str(bool(live)).lower()
    resp = requests.get(
        f"{client.base_url}/api/v1/markets", params=params,
        headers=client._headers(), timeout=client.timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_market(client: "SynthesisClient", condition_id: str) -> Any:
    resp = requests.get(
        f"{client.base_url}/api/v1/polymarket/market/{condition_id}",
        headers=client._headers(), timeout=client.timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _market_dicts(item: Any) -> List[dict]:
    if not isinstance(item, dict):
        return []
    if isinstance(item.get("markets"), list):
        return [m for m in item["markets"] if isinstance(m, dict)]
    if isinstance(item.get("market"), dict):
        return [item["market"]]
    if any(k in item for k in ("left_outcome", "condition_id", "conditionId")):
        return [item]
    return []


def _extract_market(payload: Any, condition_id: Optional[str] = None) -> Optional[dict]:
    body = _unwrap(payload)
    candidates: List[dict] = []
    for item in (body if isinstance(body, list) else [body]):
        candidates.extend(_market_dicts(item))
    for m in candidates:
        cid = m.get("condition_id") or m.get("conditionId")
        if condition_id is None or str(cid) == str(condition_id):
            return m
    return candidates[0] if candidates else None


def parse_resolution(payload: Any, condition_id: Optional[str] = None) -> Optional[str]:
    """Return the winning side ("Up"/"Down") from a Synthesis market, else None.

    Uses ``resolved`` + ``winner_token_id`` (mapped via left/right token ids),
    falling back to ``left_price``/``right_price`` hitting ~1.
    """
    m = _extract_market(payload, condition_id)
    if not m or not m.get("resolved"):
        return None
    left_out, left_tok = m.get("left_outcome"), str(m.get("left_token_id") or "")
    right_out, right_tok = m.get("right_outcome"), str(m.get("right_token_id") or "")
    winner = m.get("winner_token_id")
    if winner:
        w = str(winner)
        if w == left_tok and left_out:
            return str(left_out)
        if w == right_tok and right_out:
            return str(right_out)
    if _num(m.get("left_price")) >= 0.99 and left_out:
        return str(left_out)
    if _num(m.get("right_price")) >= 0.99 and right_out:
        return str(right_out)
    return None


def parse_markets(payload: Any, now: Optional[dt.datetime] = None,
                  window_seconds: Optional[int] = 300, tol: int = 60) -> List["pm.Window"]:
    """Turn a Synthesis markets response into Bitcoin Up/Down 5-min windows.

    Reuses ``polybot.polymarket`` title parsing; builds each window's token_map
    directly from the ``left/right_outcome`` + ``left/right_token_id`` fields.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    now_et = now.astimezone(pm.ET)
    windows: List[pm.Window] = []
    for event in _as_list(_unwrap(payload)):
        if not isinstance(event, dict):
            continue
        markets = event.get("markets") if isinstance(event.get("markets"), list) else [event]
        for m in markets:
            if not isinstance(m, dict):
                continue
            question = m.get("question") or m.get("title") or ""
            if "bitcoin up or down" not in question.lower():
                continue
            if m.get("resolved"):
                continue
            parsed = pm.parse_window_title(question, now_et)
            if not parsed:
                continue
            start, end = parsed
            if window_seconds is not None and abs((end - start).total_seconds() - window_seconds) > tol:
                continue
            cond = m.get("condition_id") or m.get("conditionId")
            token_map: Dict[str, str] = {}
            for out_key, tok_key in (("left_outcome", "left_token_id"), ("right_outcome", "right_token_id")):
                out, tok = m.get(out_key), m.get(tok_key)
                if out and tok:
                    token_map[str(out)] = str(tok)
            if not cond or token_map.get("Up") is None or token_map.get("Down") is None:
                continue
            windows.append(pm.Window(condition_id=str(cond), title=question,
                                     start=start, end=end, token_map=token_map, raw=m))
    return windows


def parse_orderbook(payload: Any) -> Dict[str, List]:
    """Convert a Synthesis orderbook (bids/asks price->size maps) to our shape."""
    body = _unwrap(payload)
    entries = _as_list(body)
    entry = entries[0] if entries else body
    ob = entry.get("orderbook", entry) if isinstance(entry, dict) else {}
    if not isinstance(ob, dict):
        return {"asks": [], "bids": []}

    def _levels(m: Any, reverse: bool) -> List:
        out = []
        if isinstance(m, dict):
            for price, size in m.items():
                out.append((_num(price), _num(size)))
        elif isinstance(m, list):
            for lvl in m:
                if isinstance(lvl, dict):
                    out.append((_num(lvl.get("price")), _num(lvl.get("size"))))
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    out.append((_num(lvl[0]), _num(lvl[1])))
        out = [(p, s) for p, s in out if p > 0 and s > 0]
        out.sort(key=lambda x: x[0], reverse=reverse)
        return out

    return {"asks": _levels(ob.get("asks"), False), "bids": _levels(ob.get("bids"), True)}
