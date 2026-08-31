# Contributing

Issues and pull requests are welcome. This is a young project; the fastest way to
help is to run it against a site we have never tried and tell us what broke.

## The most useful thing you can send

**A site that defeated it.** Not a polished bug report — the raw failure. Which
site, what you asked for, which stage gave up (discover / explore / validate /
run), and what the run said. A crawler that fails on a real site is a better issue
than a feature request, because the failure is the specification.

If you got past a block yourself, that is even better: see *Anti-bot notes* below.

## Good first contributions

- **Anti-bot notes for a new site.** The thing this project accumulates is dated,
  concrete knowledge about how specific sites push back — a signed-CDN URL scheme, a
  hydration trap, a soft login gate. Add a note under `harness/memory/`, following
  the shape of the ones already there: what you observed, on what date, and what
  actually worked. Vague advice ("use a proxy") is not useful; a reproducible
  observation is.
- **Registry adapters.** `backend/src/adapters/` maps a source registry to the
  common shape. New registries — national statistics portals, domain-specific data
  catalogs — widen what discovery can find.
- **Quickstart hardening.** Run the quickstart on a machine that has never seen this
  project, on your OS, and open an issue for every step that did not work as
  written. Setup instructions rot silently because the people who wrote them never
  run them cold.

## Before opening a pull request

```bash
# each line runs from the repo root; the subshell keeps `cd` from leaking
(cd backend && pip install -c constraints.txt -e ".[dev]" && pytest)
(cd harness  && pip install -c constraints.txt -e .        && pytest)
```

The `-c constraints.txt` is not optional: the pyprojects declare only lower bounds,
and installing without the lock resolves against whatever PyPI holds today. That is
not a hypothetical — an unpinned `mcp` crossing into 2.x once left the agent running
with zero browser tools, silently, because the MCP server failed at import rather
than crashing.

Keep changes focused, and match the surrounding code — this codebase carries dense
explanatory comments on purpose, and a comment explaining *why* is worth more than
one restating *what*.

## Scope and conduct

Use it on data you are authorized to collect. Contributions whose purpose is to
defeat authentication, evade paywalls, or scrape at a volume designed to degrade a
site will be declined.

Be straightforward with each other. Disagreement about a technical decision is
welcome and expected; personal hostility is not.

## License

By contributing you agree that your contributions are licensed under
[Apache-2.0](LICENSE), the same as the project.
