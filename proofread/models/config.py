"""All model names, provider slugs, prices and role -> key mapping live here (and only here)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
DATA_DIR = REPO_ROOT / "data"
SPEND_DB = DATA_DIR / "spend.sqlite"
OPENROUTER_PRICE_CACHE = DATA_DIR / "openrouter_prices.json"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

Provider = Literal["anthropic", "openrouter"]

# Roles and their keys. Never mix roles.
PROPOSER_MODEL = "claude-opus-5-5"
SMOKE_AGENT_MODEL = "claude-sonnet-5"  # used before the S6b selection step
# Set by the pre-registered S6b selection step (also recorded in DECISIONS.md). None = use smoke model.
SELECTED_AGENT_MODEL: str | None = None
ANTHROPIC_BASELINES = ("claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5-5")
# One current flagship each from OpenAI and Google on OpenRouter (resolved 2026-09-26 from /models).
NON_ANTHROPIC_BASELINES = ("openai/gpt-5.5", "google/gemini-3.1-pro-preview")


@dataclass(frozen=True)
class AnthropicModel:
    api_id: str
    input_per_mtok: float
    output_per_mtok: float
    sampling_params: bool  # False: temperature/top_p rejected (400) on this model
    cache_read_per_mtok: float = 0.0


# Anthropic first-party prices, USD per million tokens (claude-api skill, cached 2026-06-24).
ANTHROPIC_MODELS: dict[str, AnthropicModel] = {
    "claude-opus-5-5": AnthropicModel("claude-opus-5-5", 4.0, 20.0, False, 0.20),
    "claude-sonnet-5": AnthropicModel("claude-sonnet-5", 2.0, 10.0, False, 0.20),
    "claude-haiku-4-5-20251001": AnthropicModel("claude-haiku-4-5-20251001", 1.0, 5.0, True, 0.10),
    "claude-opus-5": AnthropicModel("claude-opus-5", 5.0, 25.0, False, 0.50),
}

# Short names -> OpenRouter slugs (resolved from https://openrouter.ai/api/v1/models on 2026-09-26).
OPENROUTER_ALIASES: dict[str, str] = {
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-5-5": "anthropic/claude-opus-5.5",
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "gpt-5.5": "openai/gpt-5.5",
    "gemini-3.1-pro": "google/gemini-3.1-pro-preview",
}

# Fallback OpenRouter prices, USD per million tokens (snapshot of /models on 2026-09-26).
OPENROUTER_FALLBACK_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-5": (2.0, 10.0),
    "anthropic/claude-opus-5.5": (4.0, 20.0),
    "anthropic/claude-opus-5": (5.0, 25.0),
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
    "openai/gpt-5.5": (5.0, 30.0),
    "openai/gpt-5.4": (2.5, 15.0),
    "openai/gpt-5.4-mini": (0.75, 4.5),
    "google/gemini-3.1-pro-preview": (2.0, 12.0),
    "google/gemini-3.5-flash": (1.5, 9.0),
}
UNKNOWN_PRICE = (30.0, 180.0)  # pessimistic, so an unknown model cannot blow the cap silently

# Models whose sampling params are removed (400 if sent) even when reached through OpenRouter.
NO_SAMPLING_SLUGS = ("anthropic/claude-sonnet-5", "anthropic/claude-opus-5", "anthropic/claude-opus-5.5",
                     "anthropic/claude-fable")


# OpenRouter reasoning effort ("low" | "medium" | "high"; "" = provider default). Per-slug entries
# override per-role defaults; env PROOFREAD_REASONING_EFFORT overrides both. Default "low" (D-010):
# at the provider default Sonnet 5 spent ~$0.75 per lcbhard episode on reasoning.
OPENROUTER_REASONING_EFFORT: dict[str, str] = {}
REASONING_EFFORT_BY_ROLE: dict[str, str] = {"agent": "low", "baseline": "low", "selection": "low"}


def reasoning_effort(role: str, slug: str) -> str:
    env = os.environ.get("PROOFREAD_REASONING_EFFORT")
    if env is not None:
        return env
    if slug in OPENROUTER_REASONING_EFFORT:
        return OPENROUTER_REASONING_EFFORT[slug]
    return REASONING_EFFORT_BY_ROLE.get(role, "")


def agent_model() -> str:
    """Inner-agent model: the S6b selection if recorded, else the smoke model."""
    env = os.environ.get("PROOFREAD_AGENT_MODEL", "")
    return env or SELECTED_AGENT_MODEL or SMOKE_AGENT_MODEL


def openrouter_slug(model: str) -> str:
    return OPENROUTER_ALIASES.get(model, model)


def provider_for(role: str, model: str) -> tuple[Provider, str]:
    """Return (provider, env key name) for a role/model. Keys never cross roles."""
    if role == "proposer":
        return "anthropic", "PROPOSER_API_KEY"
    if role == "agent":
        return "openrouter", "AGENT_API_KEY"
    if role == "selection":
        return "openrouter", "OPENROUTER_API_KEY"
    if role == "baseline":
        if model in ANTHROPIC_MODELS:
            return "anthropic", "BASELINE_API_KEY"
        return "openrouter", "OPENROUTER_API_KEY"
    raise ValueError(f"unknown role {role!r}")


_or_prices: dict[str, tuple[float, float]] | None = None


def _load_or_cache() -> dict[str, tuple[float, float]]:
    try:
        d = json.loads(OPENROUTER_PRICE_CACHE.read_text())
        if time.time() - d.get("ts", 0) < 7 * 86400:
            return {k: tuple(v) for k, v in d["prices"].items()}
    except Exception:
        pass
    return {}


def refresh_openrouter_prices(timeout_s: float = 20.0) -> dict[str, tuple[float, float]]:
    """Fetch prices from OpenRouter's public models endpoint (per-token -> per-million)."""
    import httpx

    r = httpx.get(OPENROUTER_MODELS_URL, timeout=timeout_s)
    r.raise_for_status()
    prices: dict[str, tuple[float, float]] = {}
    for m in r.json().get("data", []):
        try:
            p = m["pricing"]
            prices[m["id"]] = (float(p["prompt"]) * 1e6, float(p["completion"]) * 1e6)
        except (KeyError, TypeError, ValueError):
            continue
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OPENROUTER_PRICE_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "prices": prices}))
    tmp.replace(OPENROUTER_PRICE_CACHE)
    return prices


def openrouter_price(slug: str, *, allow_network: bool = True) -> tuple[float, float]:
    """(input, output) USD per million tokens for an OpenRouter slug."""
    global _or_prices
    if _or_prices is None:
        _or_prices = _load_or_cache()
        if not _or_prices and allow_network:
            try:
                _or_prices = refresh_openrouter_prices()
            except Exception:
                _or_prices = {}
    if slug in _or_prices:
        return _or_prices[slug]
    return OPENROUTER_FALLBACK_PRICES.get(slug, UNKNOWN_PRICE)


def anthropic_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
                   cache_write: int = 0) -> float:
    m = ANTHROPIC_MODELS.get(model)
    if m is None:
        pin, pout = UNKNOWN_PRICE
        return (input_tokens * pin + output_tokens * pout) / 1e6
    return (input_tokens * m.input_per_mtok + output_tokens * m.output_per_mtok
            + cache_read * m.cache_read_per_mtok + cache_write * m.input_per_mtok * 1.25) / 1e6
