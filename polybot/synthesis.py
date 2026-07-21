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

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests


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
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

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
            raise SynthesisError(f"order rejected {resp.status_code}: {resp.text[:400]}")
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


def parse_order(data: Dict[str, Any]) -> OrderResult:
    """Map a create-order response into an :class:`OrderResult` (tolerant of shape)."""
    return OrderResult(
        order_id=str(data.get("order_id") or data.get("id") or ""),
        token_id=str(data.get("token_id") or ""),
        side=str(data.get("side") or ""),
        type=str(data.get("type") or ""),
        status=str(data.get("status") or ""),
        amount_usdc=_num(data.get("amount")),
        filled=_num(data.get("filled") if data.get("filled") is not None else data.get("amount")),
        shares=_num(data.get("shares")),
        price=_num(data.get("price")),
        fee=parse_fee(data.get("fee")),
        raw=data,
    )


def extract_usdc_balance(data: Any) -> Optional[float]:
    """Sum the USDC-family balance from a wallet-balance response.

    Handles the nested Synthesis shape
    ``{"response": {"balance": {"USDC.e": "1000", "USDC": "500"}}}`` (falling back
    to ``{"balance": {...}}`` or a flat root), summing every token whose symbol
    starts with ``USDC`` (covers native ``USDC`` and bridged ``USDC.e``, the
    Polymarket collateral). Returns None if no balance object is present.
    """
    node = data.get("response", data) if isinstance(data, dict) else {}
    balance = node.get("balance", node) if isinstance(node, dict) else {}
    if isinstance(balance, dict):
        usdc = [v for k, v in balance.items() if str(k).upper().startswith("USDC")]
        if usdc:
            return sum(_num(v) for v in usdc)
        # Flat fallback (older/simple shapes).
        flat = _first_present(balance, ("usdc", "available", "balance", "total"))
        if flat is not None:
            return _num(flat)
    return None


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
