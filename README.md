# claude-code-jira-agent

Autonomous Jira ticket triage with Claude Code. Webhook → FastAPI → containerized Claude agent → root-cause analysis posted back to the ticket. Deployable on Kubernetes.

Built on top of [`containerized-claude-code`](https://github.com/kognitaco/containerized-claude-code). This repo adds the Jira side: webhook auth, payload parsing, async agent invocation, and posting the result back as a Jira comment.

If you'd rather not run all of this yourself, [Kognita](https://kognita.co) provides the same architecture as a managed service — codebase indexing, Jira MCP, and team-wide agent access included.

---

## What it does

1. Jira fires a webhook on `jira:issue_created` (or whatever event you configure).
2. This service authenticates the call via the `X-Jira-Authentication` header (shared secret, constant-time compare).
3. It builds a prompt from the issue payload and POSTs it to your containerized Claude Code instance.
4. Claude reads the workspace, reasons over the code, and returns an analysis.
5. The service posts the analysis as a comment back on the Jira issue via REST.

That's the whole loop. ~130 lines of Python.

---

## Architecture

```
Jira ──(webhook + X-Jira-Authentication)──> claude-code-jira-agent
                                                 │
                                                 ▼
                                       containerized-claude-code
                                          (POST /ask, async)
                                                 │
                              ┌──────────────────┼──────────────────┐
                              ▼                  ▼                  ▼
                          /workspace          docs MCP            db MCP
                          (git repos)         (your RAG)         (read-only)
                                                 │
                              ┌──────────────────┘
                              ▼
                    Comment posted back on Jira issue
```

---

## Quick start

You need the [containerized Claude Code](https://github.com/kognitaco/containerized-claude-code) container running and reachable. Then:

```bash
git clone https://github.com/kognitaco/claude-code-jira-agent.git
cd claude-code-jira-agent

cp .env.example .env
# edit .env — set JIRA_WEBHOOK_SECRET, JIRA_*, and CLAUDE_AGENT_URL

docker build -t claude-code-jira-agent .
docker run --rm -p 8080:8080 --env-file .env claude-code-jira-agent
```

Smoke test the webhook:

```bash
curl -s -X POST http://localhost:8080/jira/webhook \
  -H 'content-type: application/json' \
  -H 'X-Jira-Authentication: replace-me' \
  -d '{
    "webhookEvent": "jira:issue_created",
    "issue": {
      "key": "SUP-123",
      "fields": {
        "summary": "Order total wrong for customer 8821",
        "description": "Customer reports total off by 20.00 on order 41992.",
        "reporter": { "displayName": "Test User" }
      }
    }
  }'
```

You should get `{"status":"accepted","issue_key":"SUP-123"}` immediately. The agent runs in the background and posts its analysis as a comment on `SUP-123`.

---

## Configuration

| Variable                 | Required | Purpose                                                                       |
| ------------------------ | -------- | ----------------------------------------------------------------------------- |
| `JIRA_WEBHOOK_SECRET`    | yes      | Shared secret. Jira must send this in the `X-Jira-Authentication` header.     |
| `CLAUDE_AGENT_URL`       | yes      | URL of your containerized-claude-code service (e.g. `http://claude-code:80`). |
| `JIRA_BASE_URL`          | yes      | Your Jira tenant, e.g. `https://your-tenant.atlassian.net`.                   |
| `JIRA_EMAIL`             | yes      | Email of the Jira user the bot posts as.                                      |
| `JIRA_API_TOKEN`         | yes      | API token for that user. Generate at id.atlassian.com → Security → API tokens.|
| `CLAUDE_TIMEOUT_SECONDS` | no       | Max time to wait for Claude per ticket. Default `600`.                        |

If `JIRA_BASE_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN` aren't set, the service still runs and logs the analysis to stdout instead of posting — useful for testing.

---

## Wiring up Jira

1. In Jira: **Settings → System → Webhooks → Create webhook**.
2. URL: `https://your-agent.example.com/jira/webhook`.
3. Events: `Issue → created` (start narrow; expand once it's working).
4. Filter by JQL if you want, e.g. `project = SUP AND issuetype = "Service Request"`.
5. Under "Headers" or "Custom Headers" (depending on your Jira flavor — Jira Cloud's native UI doesn't expose custom headers directly; use a webhook gateway or [Automation for Jira](https://www.atlassian.com/software/jira/automation) to add `X-Jira-Authentication: <secret>`).

For Jira Cloud without custom-header support, an alternative is to put the secret in a query string (`?token=...`) and adjust `verify_jira_auth` to read from it. Constant-time compare still applies.

---

## API

### `POST /jira/webhook`

Headers:

- `X-Jira-Authentication: <shared secret>` — required.

Body: Jira webhook payload (JSON). Issue key is read from `issue.key`. Events other than `jira:issue_created` and `issue_created` return `202` with `status: "ignored"`.

Returns `202`:

```json
{ "status": "accepted", "issue_key": "SUP-123" }
```

The triage runs in a background task; the HTTP call returns immediately so Jira's webhook retry logic doesn't double-trigger you.

### `GET /health`

Liveness probe.

---

## Kubernetes deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: claude-code-jira-agent
spec:
  replicas: 1
  selector:
    matchLabels: { app: claude-code-jira-agent }
  template:
    metadata:
      labels: { app: claude-code-jira-agent }
    spec:
      containers:
        - name: agent
          image: your-registry/claude-code-jira-agent:latest
          ports:
            - containerPort: 8080
          env:
            - name: CLAUDE_AGENT_URL
              value: "http://claude-code.default.svc.cluster.local:80"
            - name: JIRA_BASE_URL
              value: "https://your-tenant.atlassian.net"
            - name: JIRA_WEBHOOK_SECRET
              valueFrom:
                secretKeyRef: { name: jira-secrets, key: webhook-secret }
            - name: JIRA_EMAIL
              valueFrom:
                secretKeyRef: { name: jira-secrets, key: email }
            - name: JIRA_API_TOKEN
              valueFrom:
                secretKeyRef: { name: jira-secrets, key: api-token }
          readinessProbe:
            httpGet: { path: /health, port: 8080 }
            initialDelaySeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: claude-code-jira-agent
spec:
  selector: { app: claude-code-jira-agent }
  ports:
    - port: 80
      targetPort: 8080
```

Expose the service publicly via your ingress so Jira can reach it. Lock down everything except `POST /jira/webhook` at the ingress level.

---

## Security notes

- The `X-Jira-Authentication` check uses `hmac.compare_digest` — constant-time, no timing leaks.
- The secret should be ≥ 32 random bytes. Generate with `openssl rand -hex 32`.
- Rotate the secret by updating the Jira webhook config and the K8s secret together.
- The bot's Jira API token only needs comment-create permission on the target projects. Scope down.
- Don't log full webhook payloads in production — Jira payloads contain user emails and ticket bodies.

---

## What this is not

- **Not a codebase index.** Claude only sees what's in `/workspace` of the containerized-claude-code pod. For a 100+ microservice system you need a retrieval layer in front of it (build a RAG MCP, or use [Kognita](https://kognita.co)).
- **Not idempotent.** Jira retries failed webhooks. If you care about exactly-once triage, dedupe by issue key + a short TTL cache.
- **Not multi-tenant.** One deployment = one Jira tenant + one Claude workspace. Run multiple deployments for multiple projects.

---

## Related

- [containerized-claude-code](https://github.com/kognitaco/containerized-claude-code) — the agent runtime this depends on.
- [Kognita](https://kognita.co) — managed version of this whole stack.

---

## License

MIT.
