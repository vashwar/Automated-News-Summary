# Automated News Summary

Zero-UI daily news briefing pipeline: fetch RSS → summarize with LLM → render HTML → send email via Resend.

## Architecture

- `main.py` — Full pipeline (fetch, summarize, render, send) in a single file
- `templates/briefing.html` — Jinja2 email template (daily + weekly layouts)
- `.github/workflows/briefing.yml` — GitHub Actions (scheduled + manual dispatch)
- `tests/test_main.py` — pytest test suite (25 tests)

## Key decisions

- **LLM provider:** Configurable via `LLM_PROVIDER` env var. Default is `gemini` locally, `nvidia` in GitHub Actions.
- **Email:** Sent via [Resend](https://resend.com). `FROM_EMAIL` must be from a verified Resend domain.
- **Timezone:** All time windows are PST (`America/Los_Angeles`).
- **Template rendering:** LLM outputs `**bold**` markdown in summaries. The `md_bold` Jinja2 filter converts it to `<strong>` tags. If you change how the LLM formats entities, update the filter too.

## RSS Feeds

Feed URLs are in the `FEEDS` dict in `main.py`. Bangladesh feeds use The Daily Star (general + business). If feeds break, check `feed_health.json` — warnings appear after 3+ consecutive failures.

## Running

```bash
python main.py --edition morning          # send email
python main.py --edition morning --dry-run # preview HTML, no email
```

Editions: `morning`, `evening`, `weekly`

## Testing

```bash
python -m pytest tests/ -v
```

- Tests mock `_call_gemini` for LLM calls. **Always patch `main.LLM_PROVIDER` to `"gemini"` alongside the mock**, otherwise the test will call the real API based on whatever `.env` sets.
- When adding new tests for summarization, follow the same pattern:
  ```python
  @patch("main.LLM_PROVIDER", "gemini")
  @patch("main._call_gemini")
  def test_something(mock_gemini):
      mock_gemini.return_value = { ... }
  ```

## Environment variables

See `.env.example` for the full list. Required for email delivery: `RESEND_API_KEY`, `FROM_EMAIL`, `TO_EMAIL`. Required for LLM: `GEMINI_API_KEY` or `NVIDIA_API_KEY` depending on provider.

---

# gstack

- For all web browsing, always use the `/browse` skill from gstack. Never use `mcp__claude-in-chrome__*` tools.

## Available gstack skills

- `/office-hours` - Office hours
- `/plan-ceo-review` - Plan CEO review
- `/plan-eng-review` - Plan engineering review
- `/plan-design-review` - Plan design review
- `/design-consultation` - Design consultation
- `/review` - Code review
- `/ship` - Ship
- `/land-and-deploy` - Land and deploy
- `/canary` - Canary
- `/benchmark` - Benchmark
- `/browse` - Web browsing
- `/qa` - QA
- `/qa-only` - QA only
- `/design-review` - Design review
- `/setup-browser-cookies` - Setup browser cookies
- `/setup-deploy` - Setup deploy
- `/retro` - Retro
- `/investigate` - Investigate
- `/document-release` - Document release
- `/codex` - Codex
- `/cso` - CSO
- `/autoplan` - Auto plan
- `/careful` - Careful mode
- `/freeze` - Freeze
- `/guard` - Guard
- `/unfreeze` - Unfreeze
- `/gstack-upgrade` - Upgrade gstack
