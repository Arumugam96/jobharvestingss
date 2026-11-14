"""Prompts for the HarvestAgent navigation and decision loop."""
from __future__ import annotations

from app.prompts.base_prompts import agent_persona

_HARVEST_RULES = """

You are controlling a real browser via Playwright. Use the provided tools to:
1. Navigate to URLs
2. Click elements to reveal more content
3. Scroll for infinite-scroll pages
4. Extract structured data
5. Call `finish` when your goal is achieved

Rules:
- Always navigate to a URL before extracting data
- Do not visit the same URL twice
- Prefer `extract_data` over plain text analysis
- If a page blocks you or errors, try an alternative URL
- Limit yourself to the pages needed — don't over-crawl
- When you have enough data to satisfy the goal, call `finish` immediately
"""


def harvest_system_prompt() -> str:
    """System prompt for the browser harvest loop: the agent persona (with today's date,
    computed at call time so a long-running process never goes stale) plus the rules."""
    return agent_persona() + "\n" + _HARVEST_RULES


def __getattr__(name: str) -> str:
    """Keep ``HARVEST_SYSTEM_PROMPT`` importable as a module attribute, rebuilt on each
    access (prefer calling harvest_system_prompt())."""
    if name == "HARVEST_SYSTEM_PROMPT":
        return harvest_system_prompt()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_harvest_user_prompt(url: str, goal: str, strategy: str) -> str:
    """Opening user message for the tool-driven browser harvest loop.

    Interpolates the starting URL, the goal and the planned strategy text, and tells
    the agent to navigate to the URL first and call ``finish`` when done.
    """
    return f"""## Harvest Task

**Starting URL:** {url}
**Goal:** {goal}

**Planned Strategy:**
{strategy}

Begin executing the plan. Start by navigating to the starting URL.
Use the available tools and finish when you have collected all required data.
"""
