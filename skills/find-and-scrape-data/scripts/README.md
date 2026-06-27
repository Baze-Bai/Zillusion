# find-and-scrape-data — helper scripts

Small, dependency-light programs the skill (or its agent) can call directly.
They are **optional accelerators** distilled from the Zillusion backend; the
skill's `references/*.md` playbook also describes how to do each step with
vanilla tools when these aren't installed.

```bash
pip install -r requirements.txt   # httpx (+ optional playwright for --render)
```

| Script | What it does | Example |
|--------|--------------|---------|
| `discover_sources.py` | Query free registries (CKAN / OpenAlex / Hugging Face / APIs.guru) for candidate data sources. No key, no LLM. | `python discover_sources.py "air quality by city" --limit 10` |
| `probe.py` | Inspect a page/endpoint before scraping: status, json-vs-html, `<table>`/JSON-LD/`__NEXT_DATA__`, pagination params, body preview. Credential-safe. | `python probe.py https://example.com/data --render` |
| `run_and_check.py` | Run a candidate scraper, then check record count, required fields, and spot-check values against the live page. | `python run_and_check.py workflow.py --expect-fields name,price --output output.json` |

All three print JSON to stdout and progress/errors to stderr, take `--help`, and
exit non-zero on hard failure. The agent reads the JSON and decides what to do.
