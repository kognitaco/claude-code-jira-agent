import asyncio
import hmac
import json
import os
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

CLAUDE_AGENT_URL = os.environ.get("CLAUDE_AGENT_URL", "http://claude-code:80")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "600"))

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

SYSTEM_PROMPT = (
    "You are investigating a Jira issue. Read the relevant source code in /workspace, "
    "form a hypothesis about the root cause, and respond with a concise analysis: "
    "what the issue is, where in the code it likely originates, and a suggested mitigation. "
    "Do not invent files or functions. If you cannot determine the cause, say so. "
)

app = FastAPI(title="claude-code-jira-agent")


def verify_jira_auth(token: Optional[str]) -> None:
    expected = os.environ.get("JIRA_WEBHOOK_SECRET", "").strip()
    if not expected:
        raise HTTPException(500, "JIRA_WEBHOOK_SECRET is not configured")
    got = (token or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(401, "Invalid X-Jira-Authentication")


def _issue_key(payload: dict[str, Any]) -> Optional[str]:
    issue = payload.get("issue") or {}
    return issue.get("key")


def _build_prompt(payload: dict[str, Any]) -> str:
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    key = issue.get("key", "unknown")
    summary = fields.get("summary", "")
    description = fields.get("description", "") or ""
    if isinstance(description, dict):
        description = json.dumps(description)
    reporter = ((fields.get("reporter") or {}).get("displayName")) or "unknown"
    return (
        f"A new Jira ticket has been filed.\n\n"
        f"Issue key: {key}\n"
        f"Reporter: {reporter}\n"
        f"Summary: {summary}\n\n"
        f"Description:\n{description}\n\n"
        f"Investigate the codebase in /workspace and respond with a root-cause analysis."
    )


async def _invoke_claude(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=CLAUDE_TIMEOUT) as client:
        resp = await client.post(
            f"{CLAUDE_AGENT_URL}/ask",
            json={"prompt": prompt, "system_prompt": SYSTEM_PROMPT},
        )
        resp.raise_for_status()
        return resp.json().get("output", "")


async def _post_jira_comment(issue_key: str, body: str) -> None:
    if not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN):
        print(f"[{issue_key}] Jira credentials not configured; skipping comment.")
        print(body)
        return
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": body}]}
            ],
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, auth=(JIRA_EMAIL, JIRA_API_TOKEN))
        if resp.status_code >= 300:
            print(f"[{issue_key}] Jira comment failed {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()


async def _triage(payload: dict[str, Any]) -> None:
    issue_key = _issue_key(payload) or "UNKNOWN"
    try:
        prompt = _build_prompt(payload)
        output = await _invoke_claude(prompt)
        if not output.strip():
            output = "Claude returned an empty response."
        await _post_jira_comment(issue_key, output)
    except Exception as e:
        print(f"[{issue_key}] Triage failed: {e}")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/jira/webhook", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_jira_authentication: Optional[str] = Header(None, alias="X-Jira-Authentication"),
) -> dict:
    verify_jira_auth(x_jira_authentication)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e.msg}")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Webhook body must be a JSON object")

    event = payload.get("webhookEvent") or payload.get("issue_event_type_name")
    issue_key = _issue_key(payload)
    print(f"Jira webhook received: event={event} issue={issue_key}")

    if event and "issue_created" not in event and "jira:issue_created" not in event:
        return {"status": "ignored", "reason": f"event {event} not handled"}
    if not issue_key:
        raise HTTPException(400, "Missing issue key")

    background_tasks.add_task(_triage, payload)
    return {"status": "accepted", "issue_key": issue_key}
