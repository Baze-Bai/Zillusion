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

## Known hardening backlog

The following are tracked for the OSS hardening pass (they matter most if you
expose the service):

- Optional shared `X-API-Key` gate for the API surface.
- Strict allow-list validation + path-containment on `site_id` / `query_id`
  path parameters.
- `Origin` validation on the takeover WebSocket.

## Reporting

Found a vulnerability? Please open a private security advisory on the repository
rather than a public issue.
