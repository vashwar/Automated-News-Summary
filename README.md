# Automated News Summary

A zero-UI daily news briefing pipeline that fetches articles from RSS feeds, summarizes them with an LLM, and delivers a polished HTML email to your inbox — fully automated via GitHub Actions.

## What It Does

The pipeline runs on a schedule (or on demand) and performs four steps:

1. **Fetch** — Pulls articles from RSS feeds across three categories: World News, Finance News, and Bangladesh News
2. **Summarize** — Sends the articles to an LLM (Gemini or NVIDIA) which produces a structured briefing with macro analysis
3. **Render** — Generates a clean HTML email from a Jinja2 template
4. **Send** — Delivers the email via [Resend](https://resend.com)

### Editions

| Edition | Schedule (PST) | Coverage Window |
|---------|---------------|-----------------|
| **Morning** | 8:00 AM, Mon–Fri | Prior 14 hours (6 PM → 8 AM) |
| **Evening** | 6:00 PM, Mon–Fri | Prior 10 hours (8 AM → 6 PM) |
| **Weekly** | Saturday 8:00 AM | Week-long trend digest + fresh 24h articles |

### News Sources

- **World News** — Wall Street Journal, New York Times
- **Finance News** — Wall Street Journal Markets
- **Bangladesh News** — The Daily Star (general news + business)

### Features

- **Deduplication** — Near-duplicate articles are filtered using sequence matching (80% similarity threshold)
- **Fallback windows** — If a category has no articles, the time window auto-expands (2x, then 4x)
- **Feed health tracking** — Consecutive feed failures are tracked; warnings appear in the email footer after 3+ failures
- **Weekly digest** — Saturday edition synthesizes the week's daily micro-summaries into trend analysis with a "What to Watch" section
- **Retry with notification** — Failed runs retry once after 5 minutes; if both attempts fail, an error email is sent
- **Dry run mode** — Preview the rendered HTML locally without sending email

## Project Structure

```
.
├── main.py                          # Full pipeline: fetch, summarize, render, send
├── templates/
│   └── briefing.html                # Jinja2 email template (daily + weekly layouts)
├── requirements.txt                 # Python dependencies
├── .env.example                     # Environment variable template
├── .github/
│   └── workflows/
│       └── briefing.yml             # GitHub Actions workflow (scheduled + manual)
├── feed_health.json                 # (generated) Feed failure tracking
└── weekly_state.json                # (generated) Daily micro-summaries for weekly digest
```

## Setup

### Prerequisites

- Python 3.12+
- An LLM API key ([Google Gemini](https://ai.google.dev/) or [NVIDIA NIM](https://build.nvidia.com/))
- A [Resend](https://resend.com) account with a verified sending domain
- A destination email address

### 1. Clone the repository

```bash
git clone https://github.com/vashwar/Automated-News-Summary.git
cd Automated-News-Summary
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```env
# LLM Provider: "gemini" (default) or "nvidia"
LLM_PROVIDER=gemini

# Gemini (default provider)
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-2.0-flash          # optional, this is the default

# NVIDIA (alternative provider)
NVIDIA_API_KEY=nvapi-...
NVIDIA_MODEL=meta/llama-3.1-70b-instruct  # optional, this is the default

# Email delivery (Resend)
RESEND_API_KEY=re_...
TO_EMAIL=you@example.com
FROM_EMAIL=briefing@yourdomain.com      # must be from a verified Resend domain
SEND_ERROR_EMAILS=true                  # send failure notifications (default: true)
```

You only need the API key for your chosen provider (Gemini **or** NVIDIA, not both).

## Running Locally

### Send a briefing email

```bash
python main.py --edition morning
```

Options: `morning`, `evening`, `weekly`

### Dry run (preview HTML without sending)

```bash
python main.py --edition morning --dry-run
```

This prints the rendered HTML to stdout. You can pipe it to a file to preview in a browser:

```bash
python main.py --edition morning --dry-run > preview.html
```

### Automate locally with cron (Linux/macOS)

Open your crontab:

```bash
crontab -e
```

Add entries for the editions you want (adjust the path to your project):

```cron
# Morning edition at 8 AM PST (16:00 UTC), Mon-Fri
0 16 * * 1-5 cd /path/to/Automated-News-Summary && /path/to/python main.py --edition morning

# Evening edition at 6 PM PST (02:00 UTC next day), Mon-Fri
0 2 * * 2-6 cd /path/to/Automated-News-Summary && /path/to/python main.py --edition evening

# Weekly digest at Saturday 8 AM PST (16:00 UTC)
0 16 * * 6 cd /path/to/Automated-News-Summary && /path/to/python main.py --edition weekly
```

### Automate locally with Task Scheduler (Windows)

1. Open **Task Scheduler** and click **Create Basic Task**
2. Set the trigger to **Daily** and configure your desired time
3. Set the action to **Start a program**:
   - Program: `python` (or full path to `python.exe`)
   - Arguments: `main.py --edition morning`
   - Start in: `C:\path\to\Automated-News-Summary`
4. Repeat for each edition as needed

## Automating with GitHub Actions

The included workflow (`.github/workflows/briefing.yml`) runs the pipeline on a schedule and supports manual triggers.

### 1. Add repository secrets

Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** and add:

| Secret | Description |
|--------|-------------|
| `GEMINI_API_KEY` | Your Google Gemini API key |
| `NVIDIA_API_KEY` | Your NVIDIA NIM API key (only if using NVIDIA) |
| `LLM_PROVIDER` | `gemini` or `nvidia` (optional, defaults to `gemini`) |
| `RESEND_API_KEY` | Your Resend API key |
| `TO_EMAIL` | Destination email address |
| `FROM_EMAIL` | Sender address (must be from a verified Resend domain) |

> **Tip:** When pasting secret values, make sure there are no trailing spaces or newlines — these can cause `Invalid header value` errors.

### 2. Enable the workflow

The workflow is already configured in the repository. Once you push to `main` with the secrets set, it will run automatically on schedule:

- **Morning** — 8:00 AM PST, Mon–Fri (`0 16 * * 1-5` UTC)
- **Evening** — 6:00 PM PST, Mon–Fri (`0 2 * * 2-6` UTC)
- **Weekly** — Saturday 8:00 AM PST (`0 16 * * 6` UTC)

### 3. Run manually

Go to **Actions** → **Daily News Briefing** → **Run workflow**, select an edition, and click **Run workflow**.

### State caching

The workflow uses `actions/cache` to persist `feed_health.json` and `weekly_state.json` between runs. This enables:

- Feed health tracking across runs (warns you when a source is consistently failing)
- Weekly digest compilation (daily micro-summaries are accumulated throughout the week)

## LLM Providers

### Google Gemini (default)

- Uses `gemini-2.0-flash` by default
- Supports structured JSON output via `response_schema`
- Get an API key at [ai.google.dev](https://ai.google.dev/)

### NVIDIA NIM

- Uses `meta/llama-3.1-70b-instruct` by default
- OpenAI-compatible API with JSON mode
- Get an API key at [build.nvidia.com](https://build.nvidia.com/)

To switch providers, set `LLM_PROVIDER=nvidia` in your `.env` file or GitHub secret.

## Customization

### Changing news sources

Edit the `FEEDS` dictionary in `main.py`:

```python
FEEDS = {
    "World News": [
        "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    ],
    "Finance News": [
        "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    ],
    "Bangladesh News": [
        "https://www.thedailystar.net/news/bangladesh/rss.xml",
        "https://www.thedailystar.net/business/rss.xml",
    ],
}
```

Add, remove, or rename categories as needed. The LLM prompt and email template will need corresponding updates if you change category names.

### Changing the email template

Edit `templates/briefing.html`. The template uses Jinja2 and receives:

- `data` — the structured JSON from the LLM
- `edition_type` — `morning`, `evening`, or `weekly`
- `edition_label` — human-readable edition name
- `date_str` — formatted date string
- `feed_warnings` — list of feed health warning strings

## License

MIT
