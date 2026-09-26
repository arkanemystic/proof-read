"""Model clients for Anthropic (Messages API) and OpenRouter (OpenAI-compatible), async via httpx.

make_client(role, model) picks the provider and key for the role (see config.provider_for). Every
call checks the SpendLedger caps first and records its cost afterwards. Keys are loaded from
/home/dev/work/proofread/.env with python-dotenv without exporting anything to os.environ, and are
never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import httpx
from dotenv import dotenv_values

from proofread.contracts import BudgetExceeded, ChatMessage, ModelResponse, Role, ToolCall, ToolSpec

from . import config
from .ledger import SpendLedger, default_ledger

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class ModelError(RuntimeError):
    pass


def _env() -> dict[str, str | None]:
    return dotenv_values(config.ENV_PATH) if config.ENV_PATH.exists() else {}


def load_key(name: str) -> str:
    key = (_env().get(name) or "").strip()
    if not key:
        raise ModelError(f"{name} missing in .env")
    return key


def load_workspace_id(key_name: str) -> str:
    """Anthropic keys not scoped to a workspace need the anthropic-workspace-id header.

    Looked up in .env as <ROLE>_WORKSPACE_ID (e.g. PROPOSER_WORKSPACE_ID) then ANTHROPIC_WORKSPACE_ID.
    """
    env = _env()
    role_var = key_name.removesuffix("_API_KEY") + "_WORKSPACE_ID"
    return (env.get(role_var) or env.get("ANTHROPIC_WORKSPACE_ID") or "").strip()


class _BaseClient:
    provider = ""

    def __init__(self, role: Role, model: str, api_key: str, ledger: SpendLedger | None = None,
                 timeout_s: float = 300.0, max_attempts: int = 6, transport: httpx.AsyncBaseTransport | None = None,
                 episode_id: str = "") -> None:
        self.role = role
        self.model = model
        self._key = api_key
        self.ledger = ledger or default_ledger()
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self._transport = transport
        self.episode_id = episode_id
        self.spent_usd = 0.0

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(role={self.role!r}, model={self.model!r})"

    async def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        last = ""
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as http:
            for attempt in range(self.max_attempts):
                try:
                    r = await http.post(url, headers=headers, json=body)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last = f"{type(e).__name__}"
                else:
                    if r.status_code < 300:
                        return r.json()
                    last = f"HTTP {r.status_code}: {r.text[:500]}"
                    if r.status_code not in RETRY_STATUS:
                        raise ModelError(f"{self.provider} {self.model}: {last}")
                    ra = r.headers.get("retry-after")
                    if ra:
                        try:
                            await asyncio.sleep(min(float(ra), 60.0))
                            continue
                        except ValueError:
                            pass
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(2 ** attempt + random.random(), 60.0))
        raise ModelError(f"{self.provider} {self.model}: gave up after {self.max_attempts} attempts: {last}")

    def _account(self, resp: ModelResponse, budget_key: str) -> ModelResponse:
        self.spent_usd += resp.cost_usd
        self.ledger.record(role=self.role, model=self.model, budget_key=budget_key,
                           input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                           cost_usd=resp.cost_usd, episode_id=self.episode_id)
        return resp


def _assistant_key(m: ChatMessage) -> str:
    return json.dumps([m.content, [tc.id for tc in m.tool_calls]])


class AnthropicClient(_BaseClient):
    provider = "anthropic"

    def __init__(self, *a: Any, workspace_id: str = "", **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.workspace_id = workspace_id
        # Raw assistant content (incl. thinking blocks) keyed by (text, tool ids) so history is
        # replayed verbatim; editing earlier turns would invalidate thinking blocks.
        self._raw: dict[str, list[dict[str, Any]]] = {}

    def _convert(self, messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        def push_user(block: dict[str, Any]) -> None:
            if out and out[-1]["role"] == "user":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})

        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "user":
                push_user({"type": "text", "text": m.content or "(empty)"})
            elif m.role == "tool":
                push_user({"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content or "(no output)"})
            else:
                raw = self._raw.get(_assistant_key(m))
                if raw is None:
                    raw = []
                    if m.content:
                        raw.append({"type": "text", "text": m.content})
                    for tc in m.tool_calls:
                        raw.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                    if not raw:
                        raw = [{"type": "text", "text": "(no output)"}]
                out.append({"role": "assistant", "content": raw})
        return "\n\n".join(p for p in system_parts if p), out

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None, max_tokens: int = 4096,
                       temperature: float = 0.0, budget_key: str = "") -> ModelResponse:
        self.ledger.check(budget_key)
        spec = config.ANTHROPIC_MODELS.get(self.model)
        system, msgs = self._convert(messages)
        body: dict[str, Any] = {"model": spec.api_id if spec else self.model, "max_tokens": max_tokens, "messages": msgs}
        if system:
            body["system"] = system
        if spec is None or spec.sampling_params:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        headers = {"x-api-key": self._key, "anthropic-version": config.ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        if self.workspace_id:
            headers["anthropic-workspace-id"] = self.workspace_id
        data = await self._post(config.ANTHROPIC_URL, headers, body)
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
                 for b in content if b.get("type") == "tool_use"]
        u = data.get("usage") or {}
        cr, cw = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
        itok, otok = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        resp = ModelResponse(text=text, tool_calls=calls, input_tokens=itok + cr + cw, output_tokens=otok,
                             cost_usd=config.anthropic_cost(self.model, itok, otok, cr, cw), model=self.model,
                             stop_reason=str(data.get("stop_reason") or ""))
        self._raw[_assistant_key(ChatMessage(role="assistant", content=text, tool_calls=calls))] = content
        return self._account(resp, budget_key)


class OpenRouterClient(_BaseClient):
    provider = "openrouter"

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.slug = config.openrouter_slug(self.model)
        self._raw: dict[str, dict[str, Any]] = {}

    def _convert(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role in ("system", "user"):
                out.append({"role": m.role, "content": m.content})
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or "(no output)"})
            else:
                raw = self._raw.get(_assistant_key(m))
                if raw is None:
                    raw = {"role": "assistant", "content": m.content or ""}
                    if m.tool_calls:
                        raw["tool_calls"] = [{"id": tc.id, "type": "function", "function": {
                            "name": tc.name, "arguments": json.dumps(tc.arguments)}} for tc in m.tool_calls]
                out.append(raw)
        return out

    async def complete(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None, max_tokens: int = 4096,
                       temperature: float = 0.0, budget_key: str = "") -> ModelResponse:
        self.ledger.check(budget_key)
        body: dict[str, Any] = {"model": self.slug, "max_tokens": max_tokens, "messages": self._convert(messages),
                                "usage": {"include": True}}
        effort = config.reasoning_effort(self.role, self.slug)
        if effort:
            body["reasoning"] = {"effort": effort}
        if not any(self.slug.startswith(p) for p in config.NO_SAMPLING_SLUGS):
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}} for t in tools]
        headers = {"Authorization": f"Bearer {self._key}", "content-type": "application/json",
                   "X-Title": "proofread"}
        data = await self._post(config.OPENROUTER_URL, headers, body)
        if data.get("error"):
            raise ModelError(f"openrouter {self.slug}: {str(data['error'])[:500]}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {"_value": args}
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments", "")}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        u = data.get("usage") or {}
        itok, otok = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        cost = u.get("cost")
        if cost is None:
            pin, pout = config.openrouter_price(self.slug)
            cost = (itok * pin + otok * pout) / 1e6
        resp = ModelResponse(text=text, tool_calls=calls, input_tokens=itok, output_tokens=otok, cost_usd=float(cost),
                             model=self.model, stop_reason=str(choice.get("finish_reason") or ""))
        raw = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "reasoning", "reasoning_details")}
        raw["role"] = "assistant"
        raw.setdefault("content", "")
        self._raw[_assistant_key(ChatMessage(role="assistant", content=text, tool_calls=calls))] = raw
        return self._account(resp, budget_key)


def make_client(role: Role, model: str, *, ledger: SpendLedger | None = None, episode_id: str = "",
                transport: httpx.AsyncBaseTransport | None = None, api_key: str | None = None):
    """Pick the client for a role (D-010).

    proposer: native Anthropic if PROPOSER_WORKSPACE_ID (or ANTHROPIC_WORKSPACE_ID) is set, else the
    local claude CLI (ClaudeCliClient). baseline with a native claude-* id: native Anthropic if a
    baseline workspace ID is set, else OpenRouter with OPENROUTER_API_KEY and the resolved slug.
    """
    provider, key_name = config.provider_for(role, model)
    if provider == "anthropic":
        ws = load_workspace_id(key_name) if api_key is None else "explicit"
        if not ws:
            if role == "proposer":
                from .cli_client import ClaudeCliClient

                return ClaudeCliClient(role, model, ledger=ledger, episode_id=episode_id)
            # baseline without a workspace ID: OpenRouter, selection/non-Anthropic-baseline key
            return OpenRouterClient(role, model, load_key("OPENROUTER_API_KEY"), ledger=ledger, transport=transport,
                                    episode_id=episode_id)
        key = api_key if api_key is not None else load_key(key_name)
        return AnthropicClient(role, model, key, ledger=ledger, transport=transport, episode_id=episode_id,
                               workspace_id="" if ws == "explicit" else ws)
    key = api_key if api_key is not None else load_key(key_name)
    return OpenRouterClient(role, model, key, ledger=ledger, transport=transport, episode_id=episode_id)


__all__ = ["make_client", "AnthropicClient", "OpenRouterClient", "ModelError", "BudgetExceeded", "load_key"]
