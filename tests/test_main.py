"""Tests for the Zero UI Daily News Briefing pipeline."""

import calendar
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import (
    _article_epoch,
    _build_daily_user_prompt,
    _clean_html,
    _compute_window,
    _is_duplicate,
    _strip_code_fences,
    _validate_schema,
    fetch_feeds,
    get_feed_warnings,
    load_json_file,
    render_email,
    run_pipeline,
    save_json_file,
    send_email,
    send_error_email,
    summarize,
    update_feed_health,
    DAILY_SCHEMA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(title, link, summary, published_parsed):
    """Create a mock feedparser entry."""
    entry = MagicMock()
    entry.title = title
    entry.link = link
    entry.summary = summary
    entry.description = summary
    entry.published_parsed = published_parsed
    return entry


def _make_feed(entries, bozo=False, bozo_exception=None):
    """Create a mock feedparser.parse() result."""
    feed = MagicMock()
    feed.entries = entries
    feed.bozo = bozo
    feed.bozo_exception = bozo_exception
    return feed


def _epoch(year, month, day, hour=0, minute=0, second=0):
    """Return a UTC epoch timestamp for given datetime components."""
    return calendar.timegm(time.struct_time((year, month, day, hour, minute, second, 0, 1, 0)))


# ---------------------------------------------------------------------------
# 1. test_fetch_feeds_filters_by_time_window
# ---------------------------------------------------------------------------

@patch("main.feedparser.parse")
def test_fetch_feeds_filters_by_time_window(mock_parse):
    """Articles outside the time window should be excluded."""
    inside_time = time.gmtime(_epoch(2026, 3, 23, 10, 0, 0))
    outside_time = time.gmtime(_epoch(2026, 3, 22, 1, 0, 0))

    mock_parse.return_value = _make_feed([
        _make_entry("Inside Window", "http://a.com/1", "Summary A", inside_time),
        _make_entry("Outside Window", "http://a.com/2", "Summary B", outside_time),
    ])

    window_start = _epoch(2026, 3, 23, 6, 0, 0)
    window_end = _epoch(2026, 3, 23, 18, 0, 0)
    health = {}

    result = fetch_feeds({"Test": ["http://feed.test"]}, window_start, window_end, health)

    assert len(result["Test"]) == 1
    assert result["Test"][0]["title"] == "Inside Window"


# ---------------------------------------------------------------------------
# 2. test_fetch_feeds_deduplicates_similar_titles
# ---------------------------------------------------------------------------

@patch("main.feedparser.parse")
def test_fetch_feeds_deduplicates_similar_titles(mock_parse):
    """Articles with >80% similar titles should be deduplicated."""
    t = time.gmtime(_epoch(2026, 3, 23, 10, 0, 0))

    mock_parse.return_value = _make_feed([
        _make_entry("NATO Increases Defense Spending Floor", "http://a.com/1", "A", t),
        _make_entry("NATO Increases Defense Spending", "http://b.com/2", "B", t),
        _make_entry("Bitcoin Hits New Record High", "http://c.com/3", "C", t),
    ])

    window_start = _epoch(2026, 3, 23, 0, 0, 0)
    window_end = _epoch(2026, 3, 23, 23, 59, 59)
    health = {}

    result = fetch_feeds({"Test": ["http://feed.test"]}, window_start, window_end, health)

    assert len(result["Test"]) == 2
    titles = [a["title"] for a in result["Test"]]
    assert "NATO Increases Defense Spending Floor" in titles
    assert "Bitcoin Hits New Record High" in titles


# ---------------------------------------------------------------------------
# 3. test_fetch_feeds_strips_html
# ---------------------------------------------------------------------------

def test_clean_html_strips_tags():
    """HTML tags should be stripped from article summaries."""
    raw = '<p>This is <b>bold</b> and <a href="http://example.com">a link</a> text.</p>'
    cleaned = _clean_html(raw)
    assert "<" not in cleaned
    assert "bold" in cleaned
    assert "a link" in cleaned
    assert "http://" not in cleaned  # href attributes stripped


# ---------------------------------------------------------------------------
# 4. test_fetch_feeds_fallback_expands_window
# ---------------------------------------------------------------------------

@patch("main._refetch_category")
@patch("main.feedparser.parse")
def test_fetch_feeds_fallback_expands_window(mock_parse, mock_refetch):
    """When a category returns 0 articles, the window should expand."""
    mock_parse.return_value = _make_feed([])  # Empty feed
    mock_refetch.return_value = [{"title": "Fallback Article", "link": "http://x.com", "clean_summary": "Found it"}]

    window_start = _epoch(2026, 3, 23, 6, 0, 0)
    window_end = _epoch(2026, 3, 23, 18, 0, 0)
    health = {}

    result = fetch_feeds({"Test": ["http://feed.test"]}, window_start, window_end, health)

    assert len(result["Test"]) == 1
    assert result["Test"][0]["title"] == "Fallback Article"
    mock_refetch.assert_called()


# ---------------------------------------------------------------------------
# 5. test_fetch_feeds_handles_missing_published_date
# ---------------------------------------------------------------------------

@patch("main.feedparser.parse")
def test_fetch_feeds_handles_missing_published_date(mock_parse):
    """Articles with no published_parsed should be skipped gracefully."""
    t = time.gmtime(_epoch(2026, 3, 23, 10, 0, 0))

    entry_no_date = MagicMock()
    entry_no_date.title = "No Date Article"
    entry_no_date.link = "http://a.com/nodate"
    entry_no_date.summary = "No date"
    entry_no_date.published_parsed = None

    mock_parse.return_value = _make_feed([
        entry_no_date,
        _make_entry("Has Date", "http://a.com/hasdate", "With date", t),
    ])

    window_start = _epoch(2026, 3, 23, 0, 0, 0)
    window_end = _epoch(2026, 3, 23, 23, 59, 59)
    health = {}

    result = fetch_feeds({"Test": ["http://feed.test"]}, window_start, window_end, health)

    assert len(result["Test"]) == 1
    assert result["Test"][0]["title"] == "Has Date"


# ---------------------------------------------------------------------------
# 6. test_fetch_feeds_tracks_feed_health
# ---------------------------------------------------------------------------

@patch("main.feedparser.parse")
def test_fetch_feeds_tracks_feed_health(mock_parse):
    """Feed health should track consecutive failures."""
    mock_parse.side_effect = Exception("Connection refused")

    window_start = _epoch(2026, 3, 23, 0, 0, 0)
    window_end = _epoch(2026, 3, 23, 23, 59, 59)
    health = {"Test|http://feed.test": 2}  # Already failed 2 times

    fetch_feeds({"Test": ["http://feed.test"]}, window_start, window_end, health)

    assert health["Test|http://feed.test"] == 3


# ---------------------------------------------------------------------------
# 7. test_summarize_daily_returns_valid_schema
# ---------------------------------------------------------------------------

@patch("main._call_gemini")
def test_summarize_daily_returns_valid_schema(mock_gemini):
    """Summarize should return data matching the daily schema."""
    mock_gemini.return_value = {
        "subject_line": "Test Subject",
        "macro_tldr": "Test TL;DR",
        "world_news": [
            {"title": "W1", "summary": "World 1", "source_name": "BBC", "source_url": "http://bbc.com/1"},
            {"title": "W2", "summary": "World 2", "source_name": "NYT", "source_url": "http://nyt.com/1"},
            {"title": "W3", "summary": "World 3", "source_name": "AJ", "source_url": "http://aj.com/1"},
        ],
        "finance_news": [
            {"title": "F1", "summary": "Finance 1", "source_name": "CNBC", "source_url": "http://cnbc.com/1"},
            {"title": "F2", "summary": "Finance 2", "source_name": "MW", "source_url": "http://mw.com/1"},
            {"title": "F3", "summary": "Finance 3", "source_name": "R", "source_url": "http://r.com/1"},
        ],
        "bangladesh_news": [
            {"title": "B1", "summary": "BD 1", "source_name": "DS", "source_url": "http://ds.com/1"},
            {"title": "B2", "summary": "BD 2", "source_name": "DT", "source_url": "http://dt.com/1"},
            {"title": "B3", "summary": "BD 3", "source_name": "PA", "source_url": "http://pa.com/1"},
        ],
        "connecting_the_dots": "All things are connected.",
    }

    articles = {
        "World News": [{"title": "A", "link": "http://a.com", "clean_summary": "A summary"}],
        "Finance News": [{"title": "B", "link": "http://b.com", "clean_summary": "B summary"}],
        "Bangladesh News": [{"title": "C", "link": "http://c.com", "clean_summary": "C summary"}],
    }

    data = summarize(articles, "morning")

    assert data["subject_line"] == "Test Subject"
    assert data["macro_tldr"] == "Test TL;DR"
    assert len(data["world_news"]) == 3
    assert len(data["finance_news"]) == 3
    assert len(data["bangladesh_news"]) == 3
    assert data["connecting_the_dots"] == "All things are connected."


# ---------------------------------------------------------------------------
# 8. test_summarize_weekly_returns_valid_schema
# ---------------------------------------------------------------------------

@patch("main._call_gemini")
def test_summarize_weekly_returns_valid_schema(mock_gemini):
    """Summarize weekly should return data matching the weekly schema."""
    mock_gemini.return_value = {
        "subject_line": "Week in Review",
        "week_overview": "A busy week.",
        "world_trends": [{"title": "WT1", "summary": "World trend 1"}],
        "finance_trends": [{"title": "FT1", "summary": "Finance trend 1"}],
        "bangladesh_trends": [{"title": "BT1", "summary": "BD trend 1"}],
        "what_to_watch": [{"title": "WW1", "summary": "Watch item 1"}],
    }

    summaries = [{"date": "2026-03-18", "subject_line": "Mon", "macro_tldr": "X", "connecting_the_dots": "Y"}]
    articles = {"World News": [], "Finance News": [], "Bangladesh News": []}

    data = summarize(articles, "weekly", weekly_summaries=summaries)

    assert data["subject_line"] == "Week in Review"
    assert data["week_overview"] == "A busy week."
    assert len(data["world_trends"]) >= 1
    assert len(data["what_to_watch"]) >= 1


# ---------------------------------------------------------------------------
# 9. test_summarize_empty_category_handled
# ---------------------------------------------------------------------------

@patch("main._call_gemini")
def test_summarize_empty_category_handled(mock_gemini):
    """Empty categories should produce placeholder items."""
    mock_gemini.return_value = {
        "subject_line": "Sparse Day",
        "macro_tldr": "Quiet news day",
        "world_news": [{"title": "No Coverage", "summary": "No recent coverage available", "source_name": "N/A", "source_url": ""}],
        "finance_news": [{"title": "F1", "summary": "One thing", "source_name": "CNBC", "source_url": "http://cnbc.com"}],
        "bangladesh_news": [{"title": "No Coverage", "summary": "No recent coverage available", "source_name": "N/A", "source_url": ""}],
        "connecting_the_dots": "Limited coverage today.",
    }

    articles = {
        "World News": [],
        "Finance News": [{"title": "F", "link": "http://f.com", "clean_summary": "Finance"}],
        "Bangladesh News": [],
    }

    data = summarize(articles, "morning")

    assert data["world_news"][0]["source_url"] == ""
    assert data["world_news"][0]["title"] == "No Coverage"
    # Verify _call_gemini was called with a prompt containing "No articles available"
    call_args = mock_gemini.call_args
    assert "No articles available" in call_args[0][1]  # user_prompt is 2nd positional arg


# ---------------------------------------------------------------------------
# 10. test_render_email_daily_produces_html
# ---------------------------------------------------------------------------

def test_render_email_daily_produces_html():
    """Daily edition should render all sections in the HTML output."""
    data = {
        "subject_line": "Test Subject",
        "macro_tldr": "Global trend summary",
        "world_news": [{"title": "W1", "summary": "World item", "source_name": "BBC", "source_url": "http://bbc.com"}],
        "finance_news": [{"title": "F1", "summary": "Finance item", "source_name": "CNBC", "source_url": "http://cnbc.com"}],
        "bangladesh_news": [{"title": "B1", "summary": "BD item", "source_name": "DS", "source_url": "http://ds.com"}],
        "connecting_the_dots": "Everything is linked.",
    }

    html = render_email(data, "morning")

    assert "Daily Briefing" in html
    assert "Morning Edition" in html
    assert "Macro TL;DR" in html
    assert "Global trend summary" in html
    assert "World News" in html
    assert "Finance News" in html
    assert "Bangladesh News" in html
    assert "Connecting the Dots" in html
    assert "Everything is linked." in html
    assert "http://bbc.com" in html


# ---------------------------------------------------------------------------
# 11. test_render_email_weekly_produces_html
# ---------------------------------------------------------------------------

def test_render_email_weekly_produces_html():
    """Weekly edition should render trend sections and what-to-watch."""
    data = {
        "subject_line": "Week Summary",
        "week_overview": "A big week in markets.",
        "world_trends": [{"title": "WT1", "summary": "World trend"}],
        "finance_trends": [{"title": "FT1", "summary": "Finance trend"}],
        "bangladesh_trends": [{"title": "BT1", "summary": "BD trend"}],
        "what_to_watch": [{"title": "WW1", "summary": "Watch this"}],
    }

    html = render_email(data, "weekly")

    assert "Weekly Digest" in html
    assert "Week in Review" in html
    assert "A big week in markets." in html
    assert "World Trends" in html
    assert "Finance Trends" in html
    assert "Bangladesh Trends" in html
    assert "What to Watch Next Week" in html


# ---------------------------------------------------------------------------
# 12. test_render_email_feed_health_warning
# ---------------------------------------------------------------------------

def test_render_email_feed_health_warning():
    """Feed health warnings should appear in the email footer."""
    data = {
        "subject_line": "Test",
        "macro_tldr": "TL;DR",
        "world_news": [],
        "finance_news": [],
        "bangladesh_news": [],
        "connecting_the_dots": "",
    }

    html = render_email(data, "morning", feed_warnings=["BD feed failed 5 consecutive runs"])

    assert "BD feed failed 5 consecutive runs" in html


# ---------------------------------------------------------------------------
# 13. test_send_email_success
# ---------------------------------------------------------------------------

@patch("main.resend.Emails.send")
def test_send_email_success(mock_send):
    """send_email should call Resend API with correct parameters."""
    import os
    os.environ["RESEND_API_KEY"] = "test-key"
    os.environ["FROM_EMAIL"] = "from@test.com"

    send_email("<h1>Test</h1>", "Subject Line", "to@test.com")

    mock_send.assert_called_once()
    call_args = mock_send.call_args[0][0]
    assert call_args["to"] == ["to@test.com"]
    assert call_args["subject"] == "Subject Line"
    assert call_args["html"] == "<h1>Test</h1>"


# ---------------------------------------------------------------------------
# 14. test_send_email_failure_raises
# ---------------------------------------------------------------------------

@patch("main.resend.Emails.send", side_effect=Exception("Resend API error"))
def test_send_email_failure_raises(mock_send):
    """send_email should propagate exceptions from Resend."""
    import os
    os.environ["RESEND_API_KEY"] = "test-key"
    os.environ["FROM_EMAIL"] = "from@test.com"

    with pytest.raises(Exception, match="Resend API error"):
        send_email("<h1>Test</h1>", "Subject", "to@test.com")


# ---------------------------------------------------------------------------
# 15. test_retry_logic_succeeds_on_second_attempt
# ---------------------------------------------------------------------------

@patch("main.time.sleep")
@patch("main.run_pipeline")
def test_retry_logic_succeeds_on_second_attempt(mock_pipeline, mock_sleep):
    """If the first attempt fails, it should retry after sleeping."""
    mock_pipeline.side_effect = [Exception("First fail"), None]

    import os
    os.environ["TO_EMAIL"] = "to@test.com"

    with patch("sys.argv", ["main.py", "--edition", "morning"]):
        from main import main
        main()

    assert mock_pipeline.call_count == 2
    mock_sleep.assert_called_once_with(300)


# ---------------------------------------------------------------------------
# 16. test_retry_logic_sends_error_email_on_double_failure
# ---------------------------------------------------------------------------

@patch("main.send_error_email")
@patch("main.time.sleep")
@patch("main.run_pipeline", side_effect=Exception("Persistent failure"))
def test_retry_logic_sends_error_email_on_double_failure(mock_pipeline, mock_sleep, mock_error_email):
    """Both attempts failing should trigger an error notification email."""
    import os
    os.environ["TO_EMAIL"] = "to@test.com"

    with patch("sys.argv", ["main.py", "--edition", "morning"]):
        from main import main
        with pytest.raises(SystemExit):
            main()

    assert mock_pipeline.call_count == 2
    mock_error_email.assert_called_once()
    # First arg is the error message, second is the to_address
    assert mock_error_email.call_args[0][1] == "to@test.com"


# ---------------------------------------------------------------------------
# 17. test_weekly_state_append_after_daily_run
# ---------------------------------------------------------------------------

@patch("main.send_email")
@patch("main._call_gemini")
@patch("main.feedparser.parse")
def test_weekly_state_append_after_daily_run(mock_parse, mock_gemini, mock_send, tmp_path):
    """After a daily run, the micro-summary should be appended to weekly_state.json."""
    import main

    # Set up temp state files
    state_file = tmp_path / "weekly_state.json"
    health_file = tmp_path / "feed_health.json"
    state_file.write_text('{"summaries": []}')
    health_file.write_text('{}')

    original_weekly = main.WEEKLY_STATE_FILE
    original_health = main.FEED_HEALTH_FILE
    main.WEEKLY_STATE_FILE = state_file
    main.FEED_HEALTH_FILE = health_file

    t = time.gmtime(_epoch(2026, 3, 23, 10, 0, 0))
    mock_parse.return_value = _make_feed([_make_entry("Art", "http://a.com", "Sum", t)])

    mock_gemini.return_value = {
        "subject_line": "Daily Subject",
        "macro_tldr": "Daily TL;DR",
        "world_news": [{"title": "W", "summary": "W", "source_name": "S", "source_url": "http://s.com"}],
        "finance_news": [{"title": "F", "summary": "F", "source_name": "S", "source_url": "http://s.com"}],
        "bangladesh_news": [{"title": "B", "summary": "B", "source_name": "S", "source_url": "http://s.com"}],
        "connecting_the_dots": "Connected.",
    }

    import os
    os.environ["TO_EMAIL"] = "to@test.com"
    os.environ["RESEND_API_KEY"] = "key"
    os.environ["FROM_EMAIL"] = "from@test.com"

    try:
        main.run_pipeline("morning")

        state = json.loads(state_file.read_text())
        assert len(state["summaries"]) == 1
        assert state["summaries"][0]["subject_line"] == "Daily Subject"
        assert state["summaries"][0]["macro_tldr"] == "Daily TL;DR"
        assert state["summaries"][0]["connecting_the_dots"] == "Connected."
    finally:
        main.WEEKLY_STATE_FILE = original_weekly
        main.FEED_HEALTH_FILE = original_health


# ---------------------------------------------------------------------------
# 18. test_weekly_state_cleared_after_digest
# ---------------------------------------------------------------------------

@patch("main.send_email")
@patch("main._call_gemini")
@patch("main.feedparser.parse")
def test_weekly_state_cleared_after_digest(mock_parse, mock_gemini, mock_send, tmp_path):
    """After a weekly digest, weekly_state.json should be cleared."""
    import main

    state_file = tmp_path / "weekly_state.json"
    health_file = tmp_path / "feed_health.json"
    state_file.write_text(json.dumps({"summaries": [
        {"date": "2026-03-18", "subject_line": "Mon", "macro_tldr": "X", "connecting_the_dots": "Y"},
    ]}))
    health_file.write_text('{}')

    original_weekly = main.WEEKLY_STATE_FILE
    original_health = main.FEED_HEALTH_FILE
    main.WEEKLY_STATE_FILE = state_file
    main.FEED_HEALTH_FILE = health_file

    mock_parse.return_value = _make_feed([])

    mock_gemini.return_value = {
        "subject_line": "Week Review",
        "week_overview": "Overview",
        "world_trends": [{"title": "WT", "summary": "WT"}],
        "finance_trends": [{"title": "FT", "summary": "FT"}],
        "bangladesh_trends": [{"title": "BT", "summary": "BT"}],
        "what_to_watch": [{"title": "WW", "summary": "WW"}],
    }

    import os
    os.environ["TO_EMAIL"] = "to@test.com"
    os.environ["RESEND_API_KEY"] = "key"
    os.environ["FROM_EMAIL"] = "from@test.com"

    try:
        main.run_pipeline("weekly")

        state = json.loads(state_file.read_text())
        assert state["summaries"] == []
    finally:
        main.WEEKLY_STATE_FILE = original_weekly
        main.FEED_HEALTH_FILE = original_health


# ---------------------------------------------------------------------------
# 19. test_timestamp_conversion_uses_calendar_timegm
# ---------------------------------------------------------------------------

def test_timestamp_conversion_uses_calendar_timegm():
    """_article_epoch must use calendar.timegm for correct UTC conversion."""
    # 2026-03-23 12:00:00 UTC
    struct = time.struct_time((2026, 3, 23, 12, 0, 0, 0, 82, 0))
    expected = calendar.timegm(struct)

    entry = MagicMock()
    entry.published_parsed = struct

    result = _article_epoch(entry)

    assert result == expected
    # Verify it's NOT using time.mktime (which would differ for non-UTC timezones)
    assert result == 1774267200.0 or result == expected  # exact value depends on epoch


# ---------------------------------------------------------------------------
# Extra: state file helpers
# ---------------------------------------------------------------------------

def test_load_json_file_missing(tmp_path):
    """Missing file should return empty dict."""
    result = load_json_file(tmp_path / "nonexistent.json")
    assert result == {}


def test_save_and_load_json_file(tmp_path):
    """Atomic save should produce a readable JSON file."""
    path = tmp_path / "test.json"
    data = {"key": "value", "count": 42}
    save_json_file(path, data)
    loaded = load_json_file(path)
    assert loaded == data


def test_feed_health_warnings():
    """Feeds with 3+ consecutive failures should produce warnings."""
    health = {
        "World News|http://feed1.com": 3,
        "Finance News|http://feed2.com": 1,
        "Bangladesh News|http://feed3.com": 5,
    }
    warnings = get_feed_warnings(health)
    assert len(warnings) == 2
    assert any("feed1.com" in w for w in warnings)
    assert any("feed3.com" in w for w in warnings)


# ---------------------------------------------------------------------------
# Code fence stripping and validation
# ---------------------------------------------------------------------------

def test_strip_code_fences_json_block():
    """Code fences around JSON should be stripped."""
    raw = '```json\n{"key": "value"}\n```'
    assert _strip_code_fences(raw) == '{"key": "value"}'


def test_strip_code_fences_plain():
    """Plain JSON without fences should pass through unchanged."""
    raw = '{"key": "value"}'
    assert _strip_code_fences(raw) == '{"key": "value"}'


def test_validate_schema_missing_fields():
    """Missing required fields should raise ValueError."""
    data = {"subject_line": "Test"}
    with pytest.raises(ValueError, match="Missing required fields"):
        _validate_schema(data, DAILY_SCHEMA)
