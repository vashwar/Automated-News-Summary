"""Zero UI Daily News Briefing — fetch, summarize, deliver."""

import argparse
import calendar
import datetime
import difflib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import feedparser
import resend
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PST = ZoneInfo("America/Los_Angeles")

# LLM provider: "gemini" (default) or "nvidia"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct")
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

FEEDS = {
    "World News": [
        "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    ],
    "Finance News": [
        "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    ],
    "Bangladesh News": [
        "https://www.thedailystar.net/taxonomy/term/107/rss.xml",
        "https://en.prothomalo.com/feed",
    ],
}

DEDUP_THRESHOLD = 0.80
MAX_ARTICLES_PER_CATEGORY = 10
FEED_HEALTH_FILE = Path("feed_health.json")
WEEKLY_STATE_FILE = Path("weekly_state.json")
RETRY_DELAY_SECONDS = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("briefing")

# ---------------------------------------------------------------------------
# JSON schemas for validation
# ---------------------------------------------------------------------------

DAILY_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_line": {"type": "string"},
        "macro_tldr": {"type": "string"},
        "world_news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_name": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["title", "summary", "source_name", "source_url"],
                "additionalProperties": False,
            },
        },
        "finance_news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_name": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["title", "summary", "source_name", "source_url"],
                "additionalProperties": False,
            },
        },
        "bangladesh_news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_name": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["title", "summary", "source_name", "source_url"],
                "additionalProperties": False,
            },
        },
        "connecting_the_dots": {"type": "string"},
    },
    "required": [
        "subject_line",
        "macro_tldr",
        "world_news",
        "finance_news",
        "bangladesh_news",
        "connecting_the_dots",
    ],
    "additionalProperties": False,
}

WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_line": {"type": "string"},
        "week_overview": {"type": "string"},
        "world_trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "summary"],
                "additionalProperties": False,
            },
        },
        "finance_trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "summary"],
                "additionalProperties": False,
            },
        },
        "bangladesh_trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "summary"],
                "additionalProperties": False,
            },
        },
        "what_to_watch": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "subject_line",
        "week_overview",
        "world_trends",
        "finance_trends",
        "bangladesh_trends",
        "what_to_watch",
    ],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# State file helpers (atomic writes)
# ---------------------------------------------------------------------------


def load_json_file(path: Path) -> dict | list:
    """Load a JSON file, returning empty dict on missing or corrupt file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json_file(path: Path, data: dict | list) -> None:
    """Atomically write JSON to *path* (write-to-temp then rename)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Feed health tracking
# ---------------------------------------------------------------------------


def update_feed_health(
    health: dict, category: str, feed_url: str, success: bool
) -> None:
    """Increment or reset consecutive-failure count for a feed."""
    key = f"{category}|{feed_url}"
    if success:
        health[key] = 0
    else:
        health[key] = health.get(key, 0) + 1


def get_feed_warnings(health: dict) -> list[str]:
    """Return human-readable warnings for feeds failing 3+ consecutive runs."""
    warnings = []
    for key, count in health.items():
        if count >= 3:
            category, url = key.split("|", 1)
            warnings.append(
                f"{category} feed ({url}) has failed {count} consecutive runs"
            )
    return warnings


# ---------------------------------------------------------------------------
# fetch_feeds
# ---------------------------------------------------------------------------


def _clean_html(raw: str) -> str:
    """Strip HTML tags from article summary text."""
    return BeautifulSoup(raw or "", "html.parser").get_text(separator=" ").strip()


def _is_duplicate(title: str, existing_titles: list[str]) -> bool:
    """Check if *title* is a near-duplicate of any title in the list."""
    normalized = title.lower().strip()
    for existing in existing_titles:
        if difflib.SequenceMatcher(None, normalized, existing).ratio() > DEDUP_THRESHOLD:
            return True
    return False


def _article_epoch(entry) -> float | None:
    """Return UTC epoch seconds for an entry, or None if unparseable."""
    parsed = getattr(entry, "published_parsed", None)
    if parsed is None:
        return None
    try:
        return float(calendar.timegm(parsed))
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_feeds(
    feeds_config: dict[str, list[str]],
    window_start: float,
    window_end: float,
    health: dict,
) -> dict[str, list[dict]]:
    """Fetch and clean RSS articles within the time window.

    Returns ``{category: [{title, link, clean_summary}]}``.
    Updates *health* dict in-place with success/failure per feed.
    """
    result: dict[str, list[dict]] = {}

    for category, urls in feeds_config.items():
        articles: list[dict] = []
        seen_titles: list[str] = []

        for url in urls:
            try:
                feed = feedparser.parse(url)
                if feed.bozo and not feed.entries:
                    raise ValueError(f"Feed error: {feed.bozo_exception}")
                update_feed_health(health, category, url, success=True)
            except Exception:
                log.warning("Failed to fetch %s — %s", url, category)
                update_feed_health(health, category, url, success=False)
                continue

            for entry in feed.entries:
                epoch = _article_epoch(entry)
                if epoch is None:
                    continue
                if not (window_start <= epoch <= window_end):
                    continue

                title = getattr(entry, "title", "Untitled")
                if _is_duplicate(title, seen_titles):
                    continue
                seen_titles.append(title.lower().strip())

                articles.append({
                    "title": title,
                    "link": getattr(entry, "link", ""),
                    "clean_summary": _clean_html(
                        getattr(entry, "summary", getattr(entry, "description", ""))
                    ),
                })

                if len(articles) >= MAX_ARTICLES_PER_CATEGORY:
                    break
            if len(articles) >= MAX_ARTICLES_PER_CATEGORY:
                break

        result[category] = articles

    # Fallback: expand window for empty categories (24h then 48h)
    for category, arts in result.items():
        if arts:
            continue
        for multiplier in (2, 4):  # 24h, 48h
            span = window_end - window_start
            expanded_start = window_end - span * multiplier
            result[category] = _refetch_category(
                feeds_config[category], expanded_start, window_end, health, category
            )
            if result[category]:
                log.info(
                    "Fallback for %s: found %d articles with %dx window",
                    category, len(result[category]), multiplier,
                )
                break

    return result


def _refetch_category(
    urls: list[str],
    window_start: float,
    window_end: float,
    health: dict,
    category: str,
) -> list[dict]:
    """Re-fetch a single category with an expanded window."""
    articles: list[dict] = []
    seen_titles: list[str] = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                continue
        except Exception:
            continue
        for entry in feed.entries:
            epoch = _article_epoch(entry)
            if epoch is None:
                continue
            if not (window_start <= epoch <= window_end):
                continue
            title = getattr(entry, "title", "Untitled")
            if _is_duplicate(title, seen_titles):
                continue
            seen_titles.append(title.lower().strip())
            articles.append({
                "title": title,
                "link": getattr(entry, "link", ""),
                "clean_summary": _clean_html(
                    getattr(entry, "summary", getattr(entry, "description", ""))
                ),
            })
            if len(articles) >= MAX_ARTICLES_PER_CATEGORY:
                return articles
    return articles


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

DAILY_SYSTEM_PROMPT = """\
You are an executive briefing assistant. Your reader is a Principal Tech Lead \
currently enrolled in an executive MBA program. They require high-signal, \
macro-level insights devoid of fluff or clickbait.

Review the provided news articles and synthesize them into a structured briefing.

Rules:
- Allocate 2-4 bullet items per category based on newsworthiness (9-10 total \
across all three categories).
- Each bullet has a concise title (2-4 words) and a single synthesized sentence.
- Bold entities (companies, countries, key figures) using **bold** in summary text.
- subject_line: a compelling, information-dense email subject (under 80 chars).
- macro_tldr: single sentence synthesizing the most important global trend.
- connecting_the_dots: 2-3 sentences connecting threads across categories.
- Each source_url must come from the provided articles.
- If a category has no articles, use title "No Coverage", summary "No recent \
coverage available", source_name "N/A", source_url "".\
"""

WEEKLY_SYSTEM_PROMPT = """\
You are an executive briefing assistant producing a Saturday weekly digest. \
Your reader is a Principal Tech Lead in an executive MBA program.

You will receive micro-summaries from each daily briefing this week, plus any \
fresh Saturday articles. Synthesize the week into trend analysis.

Rules:
- subject_line: a compelling week-summary subject line (under 80 chars).
- week_overview: 3-4 sentences synthesizing the week's most important developments.
- For each trend category: 2-4 items distilling the week's patterns (not just \
repeating daily headlines). Focus on what changed, what emerged, what accelerated.
- what_to_watch: 2-3 items describing storylines to monitor next week.
- Bold entities using **bold** in text.\
"""


def _build_daily_user_prompt(articles: dict[str, list[dict]]) -> str:
    """Format fetched articles into the user prompt for the daily edition."""
    sections = []
    for category, items in articles.items():
        if not items:
            sections.append(f"## {category}\nNo articles available.\n")
            continue
        lines = [f"## {category}"]
        for art in items:
            lines.append(
                f"- [{art['title']}]({art['link']})\n  {art['clean_summary']}"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _build_weekly_user_prompt(
    weekly_summaries: list[dict], articles: dict[str, list[dict]]
) -> str:
    """Format stored micro-summaries + fresh articles for the weekly edition."""
    parts = ["## This Week's Daily Briefing Summaries\n"]
    for entry in weekly_summaries:
        parts.append(
            f"**{entry.get('date', 'Unknown date')}** — {entry.get('subject_line', '')}\n"
            f"TL;DR: {entry.get('macro_tldr', '')}\n"
            f"Connections: {entry.get('connecting_the_dots', '')}\n"
        )

    parts.append("\n## Fresh Saturday Articles\n")
    for category, items in articles.items():
        if not items:
            continue
        parts.append(f"### {category}")
        for art in items:
            parts.append(f"- [{art['title']}]({art['link']})\n  {art['clean_summary']}")
        parts.append("")

    return "\n".join(parts)


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[: -3]
    return text.strip()


def _validate_schema(data: dict, schema: dict) -> None:
    """Validate that *data* has all required keys from *schema*."""
    required = schema.get("required", [])
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Missing required fields in LLM response: {missing}")


def _strip_additional_properties(obj):
    """Recursively remove 'additionalProperties' keys unsupported by Gemini."""
    if isinstance(obj, dict):
        return {
            k: _strip_additional_properties(v)
            for k, v in obj.items()
            if k != "additionalProperties"
        }
    if isinstance(obj, list):
        return [_strip_additional_properties(item) for item in obj]
    return obj


def _call_gemini(system_prompt: str, user_prompt: str, schema: dict) -> dict:
    """Call Google Gemini API with JSON mode."""
    import google.genai as genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    gemini_schema = _strip_additional_properties(schema)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=gemini_schema,
            temperature=0.3,
        ),
    )

    text = response.text
    text = _strip_code_fences(text)
    data = json.loads(text)
    _validate_schema(data, schema)
    return data


def _call_nvidia(system_prompt: str, user_prompt: str, schema: dict) -> dict:
    """Call NVIDIA API (OpenAI-compatible) with JSON mode."""
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["NVIDIA_API_KEY"],
        base_url=NVIDIA_BASE_URL,
    )

    response = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=2048,
    )

    text = response.choices[0].message.content
    text = _strip_code_fences(text)
    data = json.loads(text)
    _validate_schema(data, schema)
    return data


def summarize(
    articles: dict[str, list[dict]],
    edition_type: str,
    weekly_summaries: list[dict] | None = None,
) -> dict:
    """Call the configured LLM provider to produce the briefing JSON."""
    if edition_type == "weekly":
        system_prompt = WEEKLY_SYSTEM_PROMPT
        user_prompt = _build_weekly_user_prompt(weekly_summaries or [], articles)
        schema = WEEKLY_SCHEMA
    else:
        system_prompt = DAILY_SYSTEM_PROMPT
        user_prompt = _build_daily_user_prompt(articles)
        schema = DAILY_SCHEMA

    # Append JSON schema instructions to the system prompt
    schema_instruction = (
        "\n\nOutput ONLY valid JSON (no markdown, no code fences) matching this schema:\n"
        + json.dumps(schema, indent=2)
    )
    full_system_prompt = system_prompt + schema_instruction

    provider = LLM_PROVIDER.lower()
    log.info("Calling %s API (%s edition)...", provider, edition_type)

    if provider == "nvidia":
        data = _call_nvidia(full_system_prompt, user_prompt, schema)
    else:
        data = _call_gemini(full_system_prompt, user_prompt, schema)

    log.info("LLM response: %s", json.dumps(data, ensure_ascii=False)[:500])
    return data


# ---------------------------------------------------------------------------
# render_email
# ---------------------------------------------------------------------------


def render_email(
    data: dict,
    edition_type: str,
    feed_warnings: list[str] | None = None,
) -> str:
    """Render the briefing data into an HTML email using the Jinja2 template."""
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=False)
    template = env.get_template("briefing.html")

    now_pst = datetime.datetime.now(PST)
    edition_label = {
        "morning": "Morning Edition",
        "evening": "Evening Edition",
        "weekly": "Weekly Digest",
    }.get(edition_type, "Edition")

    return template.render(
        data=data,
        edition_type=edition_type,
        edition_label=edition_label,
        date_str=now_pst.strftime("%A, %B %d, %Y"),
        feed_warnings=feed_warnings or [],
    )


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


def send_email(html: str, subject: str, to_address: str) -> None:
    """Send the briefing email via Resend."""
    resend.api_key = os.environ["RESEND_API_KEY"]
    params: resend.Emails.SendParams = {
        "from": os.environ["FROM_EMAIL"],
        "to": [to_address],
        "subject": subject,
        "html": html,
    }
    resend.Emails.send(params)
    log.info("Email sent to %s — subject: %s", to_address, subject)


def send_error_email(error_msg: str, to_address: str) -> None:
    """Send an error notification email."""
    if os.environ.get("SEND_ERROR_EMAILS", "true").lower() != "true":
        log.info("Error emails disabled — skipping notification")
        return
    resend.api_key = os.environ["RESEND_API_KEY"]
    params: resend.Emails.SendParams = {
        "from": os.environ["FROM_EMAIL"],
        "to": [to_address],
        "subject": "NewsSummary: Run Failed",
        "html": (
            f"<p>Your news briefing failed after two attempts.</p>"
            f"<pre>{error_msg}</pre>"
            f"<p>Check GitHub Actions logs for details.</p>"
        ),
    }
    resend.Emails.send(params)
    log.warning("Error notification sent to %s", to_address)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def _compute_window(edition_type: str) -> tuple[float, float]:
    """Return (window_start, window_end) as UTC epoch seconds.

    Morning (8 AM PST): covers prior 14 hours (6 PM yesterday → 8 AM today)
    Evening (6 PM PST): covers prior 10 hours (8 AM → 6 PM today)
    Weekly  (Sat 8 AM): covers prior 24 hours (for fresh Saturday articles)
    """
    now = datetime.datetime.now(PST)

    if edition_type == "morning":
        window_end = now.replace(hour=8, minute=0, second=0, microsecond=0)
        window_start = window_end - datetime.timedelta(hours=14)
    elif edition_type == "evening":
        window_end = now.replace(hour=18, minute=0, second=0, microsecond=0)
        window_start = window_end - datetime.timedelta(hours=10)
    else:  # weekly
        window_end = now
        window_start = now - datetime.timedelta(hours=24)

    return window_start.timestamp(), window_end.timestamp()


def run_pipeline(edition_type: str) -> None:
    """Execute the full fetch → summarize → render → send pipeline."""
    to_address = os.environ["TO_EMAIL"]

    # Load persisted state
    health = load_json_file(FEED_HEALTH_FILE)
    if not isinstance(health, dict):
        health = {}

    # Fetch articles
    window_start, window_end = _compute_window(edition_type)
    articles = fetch_feeds(FEEDS, window_start, window_end, health)

    # Save updated feed health
    save_json_file(FEED_HEALTH_FILE, health)
    feed_warnings = get_feed_warnings(health)
    if feed_warnings:
        for w in feed_warnings:
            log.warning("Feed health: %s", w)

    # Summarize
    weekly_summaries = None
    if edition_type == "weekly":
        state = load_json_file(WEEKLY_STATE_FILE)
        weekly_summaries = state.get("summaries", []) if isinstance(state, dict) else []

    data = summarize(articles, edition_type, weekly_summaries)

    # Render and send
    html = render_email(data, edition_type, feed_warnings)
    subject = data.get("subject_line", f"Daily Briefing — {edition_type.title()}")
    send_email(html, subject, to_address)

    # Update weekly state
    if edition_type in ("morning", "evening"):
        state = load_json_file(WEEKLY_STATE_FILE)
        if not isinstance(state, dict):
            state = {}
        summaries = state.get("summaries", [])
        summaries.append({
            "date": datetime.datetime.now(PST).strftime("%Y-%m-%d %H:%M"),
            "edition": edition_type,
            "subject_line": data.get("subject_line", ""),
            "macro_tldr": data.get("macro_tldr", ""),
            "connecting_the_dots": data.get("connecting_the_dots", ""),
        })
        state["summaries"] = summaries
        save_json_file(WEEKLY_STATE_FILE, state)
    elif edition_type == "weekly":
        # Clear weekly state after successful digest
        save_json_file(WEEKLY_STATE_FILE, {"summaries": []})

    log.info("Pipeline complete for %s edition", edition_type)


# ---------------------------------------------------------------------------
# Entry point with retry
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Zero UI Daily News Briefing")
    parser.add_argument(
        "--edition",
        choices=["morning", "evening", "weekly"],
        default="morning",
        help="Which edition to generate",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rendered HTML to stdout instead of sending email",
    )
    args = parser.parse_args()

    if args.dry_run:
        os.environ.setdefault("RESEND_API_KEY", "dry-run")
        os.environ.setdefault("FROM_EMAIL", "noreply@example.com")
        os.environ.setdefault("TO_EMAIL", "test@example.com")

    to_address = os.environ.get("TO_EMAIL", "")

    for attempt in (1, 2):
        try:
            if args.dry_run:
                # Dry run: fetch and summarize, print HTML, skip email
                health = load_json_file(FEED_HEALTH_FILE)
                if not isinstance(health, dict):
                    health = {}
                window_start, window_end = _compute_window(args.edition)
                articles = fetch_feeds(FEEDS, window_start, window_end, health)
                save_json_file(FEED_HEALTH_FILE, health)
                data = summarize(articles, args.edition)
                html = render_email(data, args.edition, get_feed_warnings(health))
                print(html)
                return

            run_pipeline(args.edition)
            return
        except Exception:
            log.exception("Attempt %d failed", attempt)
            if attempt == 1:
                log.info("Retrying in %d seconds...", RETRY_DELAY_SECONDS)
                time.sleep(RETRY_DELAY_SECONDS)

    # Both attempts failed — send error notification
    try:
        import traceback

        send_error_email(traceback.format_exc(), to_address)
    except Exception:
        log.exception("Failed to send error notification email")
    sys.exit(1)


if __name__ == "__main__":
    main()
