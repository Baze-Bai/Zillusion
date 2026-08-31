# Security

## Threat model: single-tenant, local-first

Zillusion ships configured for **one operator running it on their own machine**.
It has **no built-in authentication or per-user isolation**. The defaults are
chosen so that, out of the box, nothing is exposed beyond `localhost`:

- `docker-compose.yml` binds every published port to `${BIND_HOST:-127.0.0.1}`.
- No account system, no multi-tenant data separation.

This is intentional for self-hosting. It is **not** safe to expose directly to a
network or the public internet as-is.

## If you expose it beyond localhost

Setting `BIND_HOST=0.0.0.0` (or otherwise making the backend reachable) means
**anyone who can reach the port can call every endpoint** — start paid LLM runs,
read/delete sessions, trigger crawls. Before doing that you MUST add your own
protection, e.g.:

- Put it behind a reverse proxy that enforces authentication (Caddy/Nginx +
  basic-auth/OAuth, Cloudflare Access, a VPN/Tailscale, etc.), **and/or**
- Add an application-level auth layer in front of the API routes.

Treat the embedded-browser **takeover** feature as especially sensitive: it
bridges a real, possibly-authenticated browser session to the UI. Only ever use
it on a trusted, localhost-bound instance.

## Secrets

- All credentials live in `.env` / `backend/.env`, which are git-ignored. The
  `*.example` files contain **no real values** — never put real keys in them.
- `auth_state.json`, `credentials.json`, and `*.sqlite` are git-ignored; do not
  commit them.
- You bring your own LLM and service API keys; none are bundled.

### What is stored on disk, and in what form

Nothing here is encrypted at rest. On a single-tenant localhost install that is
a deliberate trade — the decryption key would have to sit on the same disk, so
it would stop nobody who can already read your files — but you should know
exactly what is lying there:

| File | Written when | Contents |
| --- | --- | --- |
| `harness/inputs/<site>/credentials.json` | you answer the credentials prompt for a keyed API | your API key, **plaintext JSON** |
| `harness/auth_state.json` | a human takeover logs into a site | that site's **live session cookies and localStorage tokens**, plaintext |

Treat both as you would the password to the account behind them. Anyone who
reads `auth_state.json` can resume your logged-in session on that site until it
expires — copying the repo directory copies them, even though git will not.

Three mechanisms keep them out of things you might share, and it is worth
knowing their edges: the agent is blocked from reading `credentials.json`
through a `PreToolUse` hook (`.claude/hooks/guard_credentials_read.py`);
downloadable run bundles exclude both filenames at any depth
(`backend/src/services/run_bundle.py`); and shipped artifacts are scanned for
credential literals — `runtime/validator_api.py` for the API route, and
`runtime/secret_scan.py` for the agentic/takeover route, where the secrets are
the live cookies rather than a key. None of that protects the files themselves.

## Hardening that is already in place

These ship enabled or available — you do not need to build them:

- **Optional shared `X-API-Key` gate** on the API surface. Set `APP_API_KEY` and
  every request must present it (`backend/src/main.py`, `_api_key_guard`).
- **Path containment** on `site_id` / `query_id`: resolved paths are checked to
  be inside the workspace root and raise otherwise
  (`backend/src/services/harness_orchestrator.py`).
- **`Origin` validation on the takeover WebSocket** against the configured CORS
  origins (`backend/src/api/routes/takeover.py`).

## Reporting

Found a vulnerability? Please open a private security advisory on the repository
rather than a public issue.
