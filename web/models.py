"""Pydantic request models for the web API (split out of web/server.py)."""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator


class ChatRequest(BaseModel):
    message: str
    share_private: bool = False
    auto_confirm: bool = False
    think: bool = True                       # Qwen3 chain-of-thought on/off
    tools: list[str] | None = None
    budget_overrides: dict | None = None
    compaction: dict | None = None           # per-run context compaction override
    parallel_tools: dict | None = None       # per-run parallel-execution override
    sampling: dict | None = None             # per-run sampler override (temperature, top_p, …)
    sub_budget: dict | None = None           # per-run sub-agent (agent.spawn) budget override
    architect_threshold: int | None = None    # complexity gate (1-10); 0/None uses config default
    history: list[dict] | None = None
    attachments: list[str] | None = None   # uploaded file ids (owner-scoped)
    project_id: str | None = None           # work inside this project's files
    conversation_id: str | None = None       # stable chat id -> per-chat scratch work_root


class ApproveRequest(BaseModel):
    confirmation_id: str
    approved: bool


class FlagRequest(BaseModel):
    comment: str = ""                          # user's own words: what went wrong
    conversation_id: str | None = None
    chat_title: str | None = None
    run_ids: list[str] = []                    # trace runs of this session
    include_private: bool = False              # opt in: admin may see message/answer content


class FlagResolveRequest(BaseModel):
    resolved: bool = True


class AnswerRequest(BaseModel):
    ask_id: str
    answers: dict   # {qid: {value: str|list, text: str}}


# Run ids are minted server-side as uuid4 hex; a saved-chat turn carrying
# anything else is forged, and rejecting it here keeps the id from ever
# reaching the filesystem (outputs/<run_id>/).
_MINTED_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")

# Usernames become path components (uploads/projects/chat-scratch owner dirs),
# so admin-created names must be a single, boring, traversal-free component.
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class TurnModel(BaseModel):
    user_message: str
    answer: str = ""
    run_id: str | None = None
    status: str | None = None
    events: list[dict] | None = None

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, v: str | None) -> str | None:
        if v is not None and not _MINTED_RUN_ID.match(v):
            raise ValueError("run_id must be a server-minted id")
        return v


class SaveChatRequest(BaseModel):
    id: str | None = None
    title: str | None = None
    turns: list[TurnModel]
    project_id: str | None = None


class RenameRequest(BaseModel):
    title: str


class CurrentChatRequest(BaseModel):
    # The client's whole active-chat snapshot (same shape the browser keeps in
    # localStorage: {id,cid,title,saved,turns:[...]}). Opaque to the server —
    # it stores the dict verbatim, so new client fields need no schema change.
    chat: dict | None = None
    active_run: str | None = None

    @field_validator("chat")
    @classmethod
    def _check_chat(cls, v: dict | None) -> dict | None:
        if v is not None and not isinstance(v.get("turns", []), list):
            raise ValueError("chat.turns must be a list")
        return v

    @field_validator("active_run")
    @classmethod
    def _check_active_run(cls, v: str | None) -> str | None:
        if v is not None and not _MINTED_RUN_ID.match(v):
            raise ValueError("active_run must be a server-minted id")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str
    code: str | None = None


class TwoFACodeRequest(BaseModel):
    code: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class BudgetDefaultsRequest(BaseModel):
    max_iterations: int | None = None
    max_wall_clock_s: int | None = None
    max_cost_usd: float | None = None
    max_total_tokens: int | None = None


class TimezoneRequest(BaseModel):
    timezone: str = ""                       # IANA name; "" = house default


class SaveChatsDefaultRequest(BaseModel):
    enabled: bool = False


class ApiTokenRequest(BaseModel):
    name: str = ""


class VoiceRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    stream: bool = False
    voice: bool = True    # False = chat client: markdown persona, thinking, normal budgets


class PromptRequest(BaseModel):
    content: str


class NewUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class PasswordRequest(BaseModel):
    password: str


class AdminFlagRequest(BaseModel):
    is_admin: bool


# ---- Studio (admin-created skills/chains/connectors/tools) ----

class StudioPutRequest(BaseModel):
    content: str                             # full artifact text (SKILL.md / YAML / .py)

class StudioValidateRequest(BaseModel):
    kind: str                                # skill | chain | connector | tool
    name: str
    content: str

class StudioDraftRequest(BaseModel):
    kind: str
    description: str                         # plain-language brief for the local drafter


# ---- Eval harness (admin eval cases, runs, proposals) ----

class EvalPutRequest(BaseModel):
    yaml: str                                # full case YAML text

class EvalValidateRequest(BaseModel):
    yaml: str

class EvalDraftRequest(BaseModel):
    prompt: str                              # plain-language brief for the local drafter

class EvalRunRequest(BaseModel):
    id: str | None = None                    # one case id …
    ids: list[str] | None = None             # … or an explicit multi-selection …
    tag: str | None = None                   # … or every case carrying this tag
    all: bool | None = None                  # … or the whole library
    skip_stable: bool | None = None          # bulk only: skip cases that passed the last 3 runs

class EvalBenchmarkVariant(BaseModel):
    label: str                               # recorded as the result's brain
    model: str | None = None                 # LiteLLM alias; None = current brain
    sampling: dict | None = None             # e.g. {"temperature": 0, "seed": 42}
    reps: int = 3                            # repetitions per case
    harness: str = "full"                    # "full" | "brain" (no delegation tools)

class EvalBenchmarkRequest(BaseModel):
    id: str | None = None                    # one case id …
    tag: str | None = None                   # … or every case carrying this tag
    variants: list[EvalBenchmarkVariant]

class EvalScheduleRequest(BaseModel):
    selector: str                            # "case:<id>" or "tag:<tag>"
    every_hours: float                       # 1 … 720 (30 d)
