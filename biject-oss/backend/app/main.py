"""
FastAPI backend orchestrator for the Lean-Agent Protocol, adapted as Proofread's local biject-api.

Routes (the subset of biject-api that proofread/verify/biject_client.py uses):
  GET  /api/health     open
  POST /api/verify     ToolCallRequest → GuardrailResultResponse
  GET  /api/policies   registry, each entry with the source_hash the lean-worker serves
  GET  /api/audit      last N audit entries (?limit=, default 200)

Auth: if BIJECT_API_KEYS (comma separated) is set, every route except /api/health needs
`Authorization: Bearer <key>`. Unset means no auth; bind to 127.0.0.1 in that case.

Removed from upstream: the frontend-facing routes (compile/register/formalize/upload/sandbox, log
SSE, audit WebSocket), the Claude back-translation of refutations, and the mock trading agent.
"""

import hashlib
import hmac
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import orchestrator
from .models import (
    AuditEntry,
    GuardrailResultResponse,
    PolicyMetadata,
    RegistryResponse,
    ToolCallRequest,
)
from .policy_registry import load_registry

LEAN_WORKER_URL = os.environ.get("LEAN_WORKER_URL", "http://lean-worker:9000").rstrip("/")
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/app/policy_data/audit.log"))
API_KEYS = [k.strip() for k in os.environ.get("BIJECT_API_KEYS", "").split(",") if k.strip()]
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8 * 1024 * 1024)))

if not API_KEYS:
    print("WARNING: BIJECT_API_KEYS unset; /api routes are unauthenticated", flush=True)


# ---------------------------------------------------------------------------- lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0),
                                         limits=httpx.Limits(max_connections=64))
    yield
    await app.state.client.aclose()


app = FastAPI(
    title="Lean-Agent Protocol Backend (Proofread)",
    version="0.2.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def limit_body(request: Request, call_next):
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    return await call_next(request)


def require_key(request: Request) -> None:
    if not API_KEYS:
        return
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not any(hmac.compare_digest(token.encode(), k.encode()) for k in API_KEYS):
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------- audit log

_audit_lock = threading.Lock()


def _append_audit(entry: AuditEntry) -> None:
    line = entry.model_dump_json() + "\n"
    with _audit_lock:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_PATH.open("a") as f:
            f.write(line)


# ---------------------------------------------------------------------------- routes


@app.get("/api/health")
async def health():
    lean_health = await orchestrator.health.get(app.state.client, LEAN_WORKER_URL, fresh=True)
    return {"backend": "ok", "lean_worker": lean_health}


@app.post("/api/verify", response_model=GuardrailResultResponse, dependencies=[Depends(require_key)])
async def api_verify(req: ToolCallRequest):
    worker_resp = await orchestrator.verify(
        tool_name=req.tool_name,
        params=req.params,
        lean_worker_url=LEAN_WORKER_URL,
        client=app.state.client,
    )

    lean_result: str = worker_resp["result"]
    verdict = {"proved": "allowed", "refuted": "blocked", "skipped": "skipped"}.get(lean_result, "error")
    explanation = worker_resp.get("explanation") or ""
    if verdict == "allowed":
        explanation = f"Action satisfies all constraints under {worker_resp['policy_id']}."

    out = GuardrailResultResponse(
        call_id=req.call_id,
        verdict=verdict,
        explanation=explanation,
        lean_trace=worker_resp.get("trace", "")[:4000],
        latency_us=worker_resp.get("latency_us", 0),
        policy_id=worker_resp.get("policy_id", "UNKNOWN"),
        conjecture=worker_resp.get("conjecture", ""),
        elab_us=worker_resp.get("elab_us"),
        failed_policies=worker_resp.get("failed_policies", []),
        policy_hash=worker_resp.get("policy_hash", ""),
        reject_code=worker_resp.get("reject_code", ""),
        goals=worker_resp.get("goals", 0),
        conjecture_sha256=worker_resp.get("conjecture_sha256", ""),
    )
    params_sha = hashlib.sha256(json.dumps(req.params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    _append_audit(AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        call_id=req.call_id,
        agent_id=req.agent_id,
        session_id=req.session_id,
        tool_name=req.tool_name,
        params_sha256=params_sha,
        verdict=verdict,
        policy_id=out.policy_id,
        failed_policies=out.failed_policies,
        lean_trace=out.lean_trace[:1000],
        explanation=explanation,
        latency_us=out.latency_us,
        conjecture_sha256=out.conjecture_sha256,
        policy_hash=out.policy_hash,
        reject_code=out.reject_code,
        elab_us=out.elab_us,
    ))
    return out


# ---------------------------------------------------------------------------- policy registry


@app.get("/api/policies", response_model=RegistryResponse, dependencies=[Depends(require_key)])
async def api_get_policies():
    """Return the full policy registry, stamped with the source hash the kernel serves."""
    await orchestrator.health.get(app.state.client, LEAN_WORKER_URL)
    served = orchestrator.health.source_hash()
    policies = {k: PolicyMetadata(**{**v, "source_hash": served}) for k, v in load_registry().items()}
    return RegistryResponse(policies=policies, count=len(policies))


# ---------------------------------------------------------------------------- audit


@app.get("/api/audit", dependencies=[Depends(require_key)])
async def api_audit_log(limit: int = 200):
    if not AUDIT_LOG_PATH.exists():
        return {"entries": []}
    lines = AUDIT_LOG_PATH.read_text().splitlines()[-max(1, min(limit, 5000)):]
    return {"entries": [json.loads(x) for x in lines if x.strip()]}
