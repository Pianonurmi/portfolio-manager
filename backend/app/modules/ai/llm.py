"""The one way to reach the local LLM: OpenAI-compatible chat on a LAN host; never a cloud endpoint (AI-01).

Built for an OpenAI-compatible AI gateway on the LAN (PM_LLM_URL): requests carry the app name
(`X-Local-AI-Source`) so the gateway can schedule them by tier, batch work can ask for the background tier,
429/503 answers are retried after `Retry-After`, and `json_schema` output is enforced by the gateway as a grammar.
Any other OpenAI-compatible server (Ollama, vLLM) works too.
"""

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.platform.config import get_settings
from app.platform.connections import get_connection, secret
from app.platform.outbound import OutboundClient, host_allowed

APP_NAME = "portfolio-manager"
RETRIES = 4
# The gateway allows 4 requests in flight per app; api and worker are separate processes, 2 each keeps us under it.
# The GPU runs one generation at a time, so more parallelism would only queue there.
IN_FLIGHT = threading.BoundedSemaphore(2)


@dataclass
class LlmConfig:
    base: str  # .../v1
    model: str
    vision_model: str
    app: str
    key: str | None
    max_tokens: int


@dataclass
class Reply:
    content: str
    model: str
    finish_reason: str
    usage: dict[str, Any]


def config(db: Session) -> LlmConfig:
    """Connection from Settings, else the defaults from the environment (PM_LLM_URL)."""
    c = get_connection(db, "llm")
    cfg = dict(c.config or {}) if c else {}
    s = get_settings()
    url = str(cfg.get("url") or s.llm_url or "").rstrip("/")
    if not url:
        raise HTTPException(404, "Connect your local LLM in Settings → Connections (OpenAI-compatible URL and model)")
    if not host_allowed(urlsplit(url).hostname or "", s.lan_allowlist):
        raise HTTPException(
            422, "The LLM host must be on the LAN allowlist (PM_LAN_ALLOWLIST); cloud endpoints are not allowed"
        )
    return LlmConfig(
        base=url if url.endswith("/v1") else url + "/v1",
        model=str(cfg.get("model") or s.llm_model),
        vision_model=str(cfg.get("vision_model") or s.llm_vision_model),
        app=str(cfg.get("app_name") or APP_NAME),
        key=(secret(c) if c and c.secret_enc else None),
        max_tokens=int(cfg.get("max_tokens") or s.llm_max_tokens),
    )


def complete(
    db: Session,
    messages: list[dict[str, Any]],
    *,
    vision: bool = False,
    schema: dict[str, Any] | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,  # None: the configured budget (PM_LLM_MAX_TOKENS)
    temperature: float = 0.1,
    background: bool = False,
    label: str | None = None,
    client: Any | None = None,
) -> Reply:
    cfg = config(db)
    model = cfg.vision_model if vision else cfg.model
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
        "messages": messages,
    }
    if schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}
    elif json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"X-Local-AI-Source": cfg.app}
    if background:
        headers["X-Local-AI-Priority"] = "background"
    if label:  # metadata only: never document content
        headers["X-Local-AI-Task-Label"] = label.encode("ascii", "replace").decode()[:60]  # headers are ASCII
    if cfg.key:
        headers["Authorization"] = f"Bearer {cfg.key}"
    http = client or OutboundClient(timeout=600)
    for attempt in range(RETRIES):
        try:
            with IN_FLIGHT:
                r = http.request("POST", f"{cfg.base}/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            # the gateway keeps generating after a broken connection: do not resubmit the same large request
            raise HTTPException(504, "The local AI did not answer in time (the GPU queue may be busy)") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"The local AI could not be reached: {exc}") from exc
        if r.status_code in (429, 503) and attempt < RETRIES - 1:
            wait = float(r.headers.get("Retry-After") or 10)
            time.sleep(min(max(wait, 1.0), 120.0))
            continue
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json().get("error", {}).get("message") or r.json().get("detail") or "")[:200]
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            raise HTTPException(502, f"LLM answered {r.status_code}{': ' + detail if detail else ''}")
        body = r.json()
        choice = body["choices"][0]
        return Reply(
            content=str(choice["message"].get("content") or ""),
            model=str(body.get("model") or model),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=dict(body.get("usage") or {}),
        )
    raise HTTPException(503, "The LLM is busy; try again later")


def chat(
    db: Session,
    messages: list[dict[str, Any]],
    *,
    json_mode: bool = False,
    client: Any | None = None,
    temperature: float = 0.1,
) -> tuple[str, str]:
    """Returns (content, model). Raises HTTPException with a user-facing message on configuration errors."""
    r = complete(db, messages, json_mode=json_mode, client=client, temperature=temperature)
    return r.content, r.model


def parse_json(text: str) -> dict[str, Any]:
    """Models sometimes wrap JSON in prose or code fences; take the outermost object."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the model did not return JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("the model did not return a JSON object")
    return data
