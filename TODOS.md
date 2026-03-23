# TODOS

Deferred items from v1 planning. Revisit after the daily system is running and stable.

---

## Briefing Archive (JSONL)

**What:** Append structured briefing JSON to a file after each run for searchable history.
**Why:** Enables future features (story arc tracking, weekly digest improvements, personal intelligence timeline). Currently the weekly digest uses micro-summaries in `weekly_state.json`; an archive would provide richer historical data.
**Effort:** S (human) → S (CC)
**Priority:** P2
**Depends on:** Stable daily pipeline running for 1+ weeks.

---

## Story Arc Tracking

**What:** Track recurring stories across days with "Day 3 of this story" markers. Detect when the same entity/topic appears in multiple briefings and surface the continuity.
**Why:** Transforms the briefing from daily snapshots into a narrative thread. The reader sees how stories evolve, not just what happened today.
**Effort:** M (human) → S (CC)
**Priority:** P2
**Depends on:** Briefing Archive (needs persistent history across runs to detect recurring topics).

---

## Reading Time Estimate

**What:** Add "~2 min read" in the email header based on word count.
**Why:** Sets expectations for the reader. One line of Python, one line in the Jinja2 template.
**Effort:** XS (human) → XS (CC)
**Priority:** P3
**Depends on:** Nothing — can be added anytime.
