"""Prompts for post-harvest data analysis and enrichment."""
from __future__ import annotations

import json
from typing import Any

from app.prompts.base_prompts import AGENT_PERSONA, truncate

ANALYSIS_SYSTEM_PROMPT = f"""{AGENT_PERSONA}

You are a data analyst. Clean, structure, and enrich harvested web data.
Always return valid JSON.
"""


def build_dedup_prompt(records: list[dict[str, Any]], goal: str) -> str:
    """User prompt asking the LLM to de-duplicate and clean records, returned as a JSON array.

    Interpolates the goal and the records as pretty JSON truncated to 6000 chars.
    """
    return f"""Remove duplicates and clean this dataset.
Goal context: {goal}

Records:
{truncate(json.dumps(records, indent=2, default=str), 6000)}

Return the deduplicated list as a JSON array.
"""


def build_summary_prompt(data: dict[str, Any], goal: str) -> str:
    """User prompt asking for a 2-3 sentence summary of a harvest result.

    Only the goal, the top-level keys of ``data`` and its ``total`` count are sent,
    not the records themselves.
    """
    return f"""Write a brief 2-3 sentence summary of these harvest results.
Goal: {goal}
Data keys: {list(data.keys())}
Record count: {data.get('total', 'unknown')}
"""


def build_enrichment_prompt(record: dict[str, Any], enrich_fields: list[str]) -> str:
    """User prompt asking the LLM to infer ``enrich_fields`` for one record.

    Interpolates the record as JSON and the field list; the reply is expected to be
    the full record as JSON with the inferred fields added.
    """
    return f"""Enrich this record by inferring the missing fields.
Record: {json.dumps(record, default=str)}
Fields to infer: {enrich_fields}
Return the full record as JSON with the enriched fields added.
"""
