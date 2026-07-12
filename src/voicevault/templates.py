from __future__ import annotations

STATEMENTS_CSV = """statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes
,sample-investor,post,https://example.com/sample-nvda-margin,2026-05-29,2026-05-30,NVDA margin watch,"NVIDIA remains strategically strong, but softer gross margin guidance can matter if expectations are stretched.",NVDA,"earnings;ai-infrastructure",mixed,short_term,medium,Sample public statement for local verification only.
"""

PROFILE_MD = """---
role_id: sample-investor
display_name: Sample Investor
status: reviewed
updated_at: 2026-05-30
source_scope: public_statements_only
---

# Role Profile

## Focus Areas

- AI infrastructure
- Earnings quality
- Valuation discipline

## Decision Frameworks

- Compare business durability with current expectations.
- Separate short-term market reaction from long-term demand.

## Investment Style

- Evidence-first public commentary review.

## Risk Preferences

- Treat guidance changes and valuation compression as explicit risks.

## Common Stances

- Mixed when strong growth meets stretched expectations.

## Representative Views

- Sample profile for local verification only.

## Easy Misreadings

- A short-term caution does not necessarily imply a long-term bearish view.

## Evidence Index

- See `statements.csv`.
"""

EXAMPLE_EVENT_MD = """---
event_id: example-nvda-margin
date: 2026-05-30
symbols:
  - NVDA
topics:
  - earnings
  - ai-infrastructure
---

# NVIDIA Margin Guidance

NVIDIA beat revenue expectations, but gross margin guidance softened. Investors are debating whether AI infrastructure demand can keep offsetting margin pressure.
"""

EXAMPLE_THESIS_MD = """---
title: AI infrastructure durability
source_url: https://example.com/sample-ai-infrastructure
published_at: 2026-05-28
symbols:
  - NVDA
topics:
  - ai-infrastructure
stance: bullish
time_horizon: long_term
confidence: medium
---

Long-term AI infrastructure demand can remain durable even when one quarter creates margin concerns.
"""

CAPTURES_README_MD = """# VoiceVault Capture Inbox

Write collector output as `.jsonl` or `.json` files in this directory, then run:

```powershell
voicevault sync --kb E:\\knowledge-base\\voicevault
```

Each JSON object can use these fields:

```json
{
  "role_id": "sample-investor",
  "platform": "x",
  "platform_user_id": "sample_handle",
  "author": "Sample Investor",
  "url": "https://example.com/post/1",
  "published_at": "2026-05-30T08:00:00Z",
  "captured_at": "2026-05-30T08:05:00Z",
  "title": "Sample public post",
  "text": "Public statement text.",
  "symbols": ["NVDA"],
  "topics": ["ai-infrastructure"]
}
```
"""
