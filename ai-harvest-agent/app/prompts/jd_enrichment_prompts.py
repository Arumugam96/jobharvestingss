"""Prompts for LLM enrichment of scraped job descriptions
(app/services/jd_llm_enrichment.py).

Two narrowly-scoped calls, both layered ON TOP of deterministic parsing:

  * contact extraction — recruiter / point-of-contact details written in the
    description (name, designation, company, emails, phones). Every value the
    model returns is re-verified against the source text by
    app/core/contact_grounding.py; anything not literally present is dropped.
  * description restructuring — ONLY for flat, run-on descriptions. The model
    may insert line breaks and "• " markers, nothing else; the caller rejects
    the result unless its word sequence equals the input's.

Both go through LLMService.extract_json(), which wraps the content as
"Extract the following data… Schema: <schema> Content: <content>".
"""
from __future__ import annotations

# Character budget for the description sent to the model. Contacts usually sit
# at the end of a JD, so an over-long description keeps its head AND its tail.
CONTACT_CONTENT_MAX_CHARS = 12_000
_CONTACT_HEAD_CHARS = 3_000
# Restructuring echoes the whole text back; longer inputs are skipped rather
# than truncated (a truncated echo can never pass the word-preservation check).
RESTRUCTURE_MAX_CHARS = 12_000


JD_CONTACT_SYSTEM_PROMPT = """You extract recruiter / point-of-contact details that are EXPLICITLY written in a job description.

Rules:
- Copy every value exactly as it is written in the text. Never guess, infer, complete, correct, translate or construct a value. If something is not written, use null (or an empty list).
- emails: every email address written in the text.
  - If an address is glued to the following word (e.g. "hr@acme.comCTC"), return only the address ("hr@acme.com").
  - If an address is obfuscated (e.g. "priya [at] acme [dot] com"), return it as a normal address ("priya@acme.com") — only when every part is written in the text.
  - type "personal" = a named person's own mailbox (e.g. priya.sharma@acme.com); type "generic" = a role/team inbox (hr@, careers@, jobs@, recruitment@, talent@, hiring@, info@, resume@, …).
  - Skip job-board / platform / system addresses (naukri.com, no-reply, report-fraud).
- phones: phone / mobile / WhatsApp numbers written in the text, exactly as written (digits, spaces, "+", "-"). Never add a country code that is not written. Skip job ids, salaries, dates and years of experience.
- recruiter_name: the person named as the contact for this job (e.g. "contact Priya Sharma"), exactly as written. null when no person is named. Never return a company name, a team name, or a placeholder like "HR Team".
- recruiter_designation: that person's title if it is written (e.g. "HR Manager"), else null.
- recruiter_company: the company that person works for, only if it is written with them, else null.
- Return only the JSON object — no commentary."""

JD_CONTACT_SCHEMA = (
    '{"recruiter_name": string or null, "recruiter_designation": string or null, '
    '"recruiter_company": string or null, '
    '"emails": [{"address": string, "type": "personal" or "generic"}], '
    '"phones": [string]}'
)


def build_contact_content(description_text: str, link_targets: list[str] | None = None) -> str:
    """The description (head + tail when over budget) plus any mailto:/tel:
    link targets, which may not be visible in the text itself."""
    text = description_text or ""
    if len(text) > CONTACT_CONTENT_MAX_CHARS:
        tail = CONTACT_CONTENT_MAX_CHARS - _CONTACT_HEAD_CHARS
        text = f"{text[:_CONTACT_HEAD_CHARS]}\n[…]\n{text[-tail:]}"
    parts = ["---JOB DESCRIPTION---", text]
    links = [t for t in (link_targets or []) if t]
    if links:
        parts += ["---LINKS IN THE DESCRIPTION---", *links]
    return "\n".join(parts)


JD_RESTRUCTURE_SYSTEM_PROMPT = """You reformat a job description that was scraped as one run-on block of text so that it is readable.

You may ONLY:
- insert line breaks and blank lines (e.g. before a section heading such as "Responsibilities:", and between paragraphs);
- put a list item on its own line starting with "• ".

You must NOT add, remove, reorder, reword, correct, translate or summarize ANY word, number or name. Every word of the input must appear in your output exactly once, in the same order. Do not add headings, numbering, emoji or markdown.

Return only the JSON object — no commentary."""

JD_RESTRUCTURE_SCHEMA = (
    '{"description": string — the SAME text, word for word, with only line breaks, '
    'blank lines and "• " list markers inserted}'
)


def build_restructure_content(description_text: str) -> str:
    return f"---JOB DESCRIPTION---\n{description_text or ''}"
