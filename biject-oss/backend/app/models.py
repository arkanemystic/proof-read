from typing import Any, Literal
from pydantic import BaseModel, Field
import uuid


class ToolCallRequest(BaseModel):
    # Identity constraints follow biject-api (see proofread/verify/BIJECT_INTERFACE.md).
    tool_name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    params: dict[str, str | int | float | bool] = Field(max_length=32)
    agent_id: str = Field(default="mock-trading-agent", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class GuardrailResultResponse(BaseModel):
    call_id: str
    verdict: str            # "allowed" | "blocked" | "skipped" | "error"
    explanation: str
    lean_trace: str
    latency_us: int
    policy_id: str
    conjecture: str = ""    # Lean goals submitted to the kernel (prefix; see conjecture_sha256)
    elab_us: int | None = None  # summed wall time of the lean processes
    failed_policies: list[str] = []  # every refuted policy (verdict "blocked")
    policy_hash: str = ""   # source hash of the compiled Lean policies that decided this call
    reject_code: str = ""   # set for verdict "error"
    goals: int = 0
    conjecture_sha256: str = ""


class AuditEntry(BaseModel):
    timestamp: str
    call_id: str
    agent_id: str
    session_id: str | None = None
    tool_name: str
    params_sha256: str      # params can hold whole files; the audit log keeps their digest
    verdict: str
    policy_id: str
    failed_policies: list[str] = []
    lean_trace: str
    explanation: str
    latency_us: int
    conjecture_sha256: str = ""
    policy_hash: str = ""
    reject_code: str = ""
    elab_us: int | None = None


# ── Policy registry schemas ──────────────────────────────────────────────────

class PolicyMetadata(BaseModel):
    policy_id: str
    display_name: str
    lean_module: str = "PolicyEnv.Basic"
    lean_namespace: str = "PolicyEnv"
    lean_function: str
    applies_to_tools: list[str]
    lean_arg_style: Literal["positional", "structure"] = "positional"
    structure_type: str = ""
    positional_map: dict[str, str] = {}
    parameter_map: dict[str, str]       # ordered: lean name → tool_param_key
    param_transforms: dict[str, str] = {}  # lean name → transform (render.py)
    param_enums: dict[str, dict[str, str]] = {}
    on_missing: Literal["skip", "error"] = "skip"
    chunk: dict[str, Any] | None = None
    lemma: str = ""         # Lean theorem justifying how goals are rendered for this policy
    source_hash: str = ""   # sha256 of the compiled Lean sources (from the lean-worker)
    description: str = ""


class RegistryResponse(BaseModel):
    policies: dict[str, PolicyMetadata]
    count: int
