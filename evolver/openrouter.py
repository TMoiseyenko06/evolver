"""Thin OpenRouter chat-completions client.

Kept deliberately small and duck-typed so tests can substitute a fake client
(anything exposing ``.chat(system, user) -> str``) with no network. Strategy
code extraction lives here too, since it is tightly coupled to how we ask the
model to format its reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests


class OpenRouterError(RuntimeError):
    pass


@dataclass
class OpenRouterClient:
    api_key: str
    model: str
    base_url: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.9
    timeout: float = 120.0
    # captured for provenance/logging by the caller
    last_prompt: dict = field(default_factory=dict)
    last_response: str = ""

    def chat(self, system: str, user: str) -> str:
        """Send a system+user message, return the assistant text."""
        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not set")
        self.last_prompt = {"system": system, "user": user, "model": self.model}
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/evolver",
                "X-Title": "evolver",
            },
        )
        if resp.status_code >= 400:
            raise OpenRouterError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise OpenRouterError(f"unexpected OpenRouter response: {data}") from exc
        self.last_response = text
        return text


_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_LINEAGE_RE = re.compile(r"^#\s*lineage:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def extract_code_blocks(text: str) -> List[str]:
    """Return every fenced code block in ``text`` (each is one strategy)."""
    blocks = [b.strip() for b in _CODE_BLOCK_RE.findall(text)]
    return [b for b in blocks if b]


def parse_lineage(source: str) -> List[str]:
    """Parse the ``# lineage: a, b`` convention we ask the model to emit.

    Returns [] for ``novel`` / missing, else the list of parent names.
    """
    m = _LINEAGE_RE.search(source)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw or raw.lower() in ("novel", "none", "-"):
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]
