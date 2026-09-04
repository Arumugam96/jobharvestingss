# LinkedIn Harvesting — Current Architecture

> Code-verified overview of the **current LinkedIn Jobs harvesting** implementation only
> (Naukri/Dice excluded). Paths are relative to `ai-harvest-agent/` unless noted.

## 1. Tech Stack

| Layer | Technology |
|---|---|
| Language / API | Python 3.11 · FastAPI 0.115 (async) + uvicorn — `app/main.py` (`create_app()`) |
| Browser automation | Playwright 1.49 (Chromium), headed under Xvfb in Docker |
| Database | PostgreSQL 16 (`postgres:16-alpine`) |
| ORM | SQLAlchemy 2.0 async + asyncpg; engine/session in `app/core/dependencies.py`; schema via `Base.metadata.create_all` + idempotent `ADD COLUMN` backfills in `main.py` |
| LLM | Ollama (Gemma:e4b) SDK (primary); optional  Anthropic Claude via `anthropic`/ OpenRouter failover — `app/services/llm_service.py` |
| Frontend | React (Vite) in `harvest-agent/`, served by nginx which reverse-proxies the API |
| Infra | `docker-compose.yml` → `postgres` + `api` + `frontend`. The `api` container also runs Xvfb + x11vnc + websockify (noVNC on 6080) via `supervisord.conf` for the live login browser; nginx (`harvest-agent/nginx.conf`) proxies API routes + `/vnc/` WebSocket |

## 2. LinkedIn Flow

```
Trigger/API → LinkedInAgent → BrowserManager (Playwright, li_at session) → LinkedIn /jobs/search
  → paginate + scroll + extract cards → per-job /jobs/view/<id>/ detail
  → LLM extract (extract_json) → recruiter enrich (LLM + Apollo) → normalize/dedup
  → HarvestRunService → Postgres (scraped_jobs / llm_calls / recruiters)
```

Two entry points converge on the same `LinkedInAgent`:

- **`POST /run-linkedin-agent`** — synchronous, LinkedIn-only; reads `harvest_config.json`, returns jobs + token usage. Guarded by `run_guard.single_flight()` (409 on concurrent run).
- **`POST /run-harvest-agent`** — async multi-source `OrchestratorAgent` (Naukri → **LinkedIn** → Dice) on a shared browser context; returns 202 + `job_id`, polled via `GET /harvest-status/{job_id}`.

## 3. Key Components

### Routes
- `app/routes/linkedin_routes.py` — `run_linkedin_agent()` (`POST /run-linkedin-agent`, no payload), `setup_linkedin_session()` (one-time manual login), `linkedin_auth_status()`, `linkedin-results` readers; `_to_scraped_job_dict()` maps scraper output → DB columns; `_mirror_run_to_db()` persists.
- `app/routes/run_harvest_agent.py` — `run_harvest_agent()` (payload `HarvestAgentRequest{config_id}`), background worker `_run_harvest_background_impl()`, stop/status endpoints.

### Agent (core) — `app/agents/linkedin_agent.py`, class `LinkedInAgent`
- `harvest(filters, headless, slow_mo)` — public entry; picks browser manager, opens page, calls `_run`.
- `_run(...)` — build URL → navigate (retry) → `_ensure_authenticated` → `_paginate_and_collect` → `_enrich_recruiters`.
- `_build_search_url()` — LinkedIn `/jobs/search/` URL with keyword/geo/`f_WT`/`f_JT`/`f_TPR`/`start=` params.
- `_paginate_and_collect()` — `&start=0,25,50…` loop; per page: stop-check, block-check, dismiss overlays, `_scroll_results`, `_extract_cards`; dedups by normalized `job_url`; `max_jobs` safety cap.
- `_extract_cards` / `_parse_card_list_view` — primary selector `[componentkey^='job-card-component-ref-']`; reads title, builds stable `/jobs/view/<id>/` URL.
- `_fetch_job_detail()` — opens each job's own `/jobs/view/<id>/` URL in a new tab (clicking the split-view card failed in prod), waits for hydration, clicks "Show more", captures description HTML.
- `_llm_fallback_extract()` — LLM structured extraction of full job + recruiter fields from stripped page text; date pre-computed in Python; per-run cap `_LLM_FALLBACK_MAX_CALLS_PER_RUN = 500`.
- `_enrich_recruiters()` → `_llm_fallback_extract_contact()` + Apollo — recruiter email/phone per `/in/` profile (cap 50); persisted via `recruiter_service`.

### Browser / Session
- `app/scrapers/browser_manager.py` — `BrowserManager` (non-persistent, accepts `storage_state`) and `PersistentBrowserManager` (`launch_persistent_context` on a Chrome profile dir; per-profile lock + stale-lock reclaim). Both inject stealth init scripts.
- `app/services/session_manager.py` — `SessionManager("linkedin")`: session file `data/sessions/linkedin_session.json`, `storage_state_arg()`, `save_session()`, `is_session_valid()`.

### Persistence
- `app/services/harvest_run_service.py`, class `HarvestRunService`: `create_run`, `bulk_insert_scraped_jobs` (resolves `recruiter_id` via `upsert_recruiter`), `replace_run_jobs` (end-of-run reconcile), `bulk_insert_llm_calls`, `list_scraped_jobs`, `scraped_job_view`.

### Normalize / Dedup
- `app/core/contact_normalize.py` — `normalize_phone`, `normalize_email`.
- `app/services/recruiter_service.py` — `normalize_linkedin_url`, `compute_dedup_key`, `upsert_recruiter`.
- `app/services/business_filter_service.py` — `_deduplicate`; flags jobs via `passed_filter`/`filter_reason` (does not drop).

## 4. Data & Persistence

All LinkedIn data lands in **shared** tables (`app/models/harvest_run.py`), distinguished by `source`:

- **`scraped_jobs`** (`ScrapedJobORM`) — the job/lead store. `run_id` FK, `source="LinkedIn"`, job fields (`job_title`, `company`, `location`, `salary`, `posted_date`, `job_url`, `job_description[_html]`, `skills`, `work_mode`…), recruiter snapshot (`job_poster_name`, `linkedin_profile_url`, `email_id`, `contact_number`), `recruiter_id` FK, pipeline state (`verification_status`, `extraction_status`, `passed_filter`), and `lead_confidence` (used only by the Feed workflow; NULL otherwise).
- **`harvest_runs`** (`HarvestRunORM`) — one row per run (`source` NULL = orchestrator); counters, `filters_snapshot`, `token_usage`, `excel_path`/`json_path`, timestamps.
- **`llm_calls`** (`LlmCallORM`) — audit log of every LLM call: `run_id`, `call_type`, `job_url`, `provider`, `model`, prompt/response, token counts, `latency_ms`, `success`.
- **`llm_types`** — *not a table.* It is `class LlmCallType` (string constants for `llm_calls.call_type`): `job_harvest`, `contact_harvest`, `feed_classify`, `feed_extract`, `email_generation`, `linkedin_generation`.
- **`recruiters`** (`RecruiterORM`, `app/models/recruiter.py`) — canonical one-row-per-person identity keyed by `dedup_key`; scraped + Apollo-enriched contact info; linked from `scraped_jobs.recruiter_id`.

DB writes are a best-effort mirror (failures swallowed so the JSON/Excel flow never breaks); jobs flush incrementally mid-scrape via an `on_batch` callback, then reconcile with `replace_run_jobs`.

## 5. LLM Usage

The current LinkedIn Jobs harvest uses the LLM as an **extraction/enrichment layer over deterministic scraping**, not as the scraper. `LLMService.extract_json(...)` is called at:

1. `_llm_fallback_extract` — parse full job description + structured fields when CSS selectors fall short (`call_type="job_harvest"`).
2. `_llm_fallback_extract_contact` — extract recruiter email/phone from profile pages (`call_type="contact_harvest"`).

Provider chain: Claude (`extraction_llm_model="claude"`) with one optional failover to Ollama/OpenRouter. Dates are pre-computed in Python (`_resolve_posted_date`). There is **no classification step** in the Jobs workflow (that is new to the Feed agent). Every call is logged to `llm_calls`.

## 6. Browser / Session

Auth is **verified, not automated** — the authoritative check is the **`li_at` cookie** in `_ensure_authenticated`. No credential login exists in the harvest path.

- **One-time setup:** `POST /linkedin-setup-session` opens a headed persistent Chrome profile; the user logs in (incl. MFA) via the live noVNC browser view; `SessionManager.save_session()` writes `data/sessions/linkedin_session.json`.
- **Reuse at harvest:** `harvest()` prefers the saved session file (`BrowserManager(storage_state=…)`); if absent, falls back to the persistent Chrome profile (`PersistentBrowserManager`).
- **Unattended vs attended:** without `li_at`, scheduled runs fail fast (`LinkedInLoginError`); interactive runs can `_wait_for_manual_login` (polls up to 600s for `li_at`).

## 7. Diagram + End-to-End

```
Frontend (React / nginx)
   POST /run-linkedin-agent (sync)  ┐
   POST /run-harvest-agent (async)  ┘──► LinkedInAgent  (app/agents/linkedin_agent.py)
        │
        ▼
   BrowserManager / PersistentBrowserManager  (Playwright Chromium, li_at session)
        │
        ▼
   LinkedIn /jobs/search ──► paginate(start=0,25,50…) + scroll + extract cards
        │
        ▼
   per-job /jobs/view/<id>/ ──► LLMService.extract_json  (job fields)
        │
        ▼
   _enrich_recruiters ──► LLM + Apollo  (recruiter contacts)
        │
        ▼
   normalize (contact_normalize) + dedup (recruiter_service / business_filter)
        │
        ▼
   HarvestRunService (bulk_insert_scraped_jobs / _llm_calls, replace_run_jobs)
        │
        ▼
   PostgreSQL:  scraped_jobs (source="LinkedIn") · harvest_runs · llm_calls · recruiters
```

**End-to-end (one line):** Trigger → LinkedInAgent → Playwright (li_at session) → LinkedIn jobs search → paginate/scroll/extract → per-job detail → LLM extract → recruiter enrich (LLM+Apollo) → normalize/dedup → HarvestRunService → Postgres.

## 8. Future Extension — LinkedIn Home Feed

The WIP Feed workflow **plugs into the existing architecture, reusing it heavily** rather than replacing it:

- **New agent:** `app/agents/linkedin_feed_agent.py` (`LinkedInFeedAgent`) — scrolls `/feed/`, then a 2-stage LLM pipeline: `_is_candidate` (deterministic IT+hiring pre-filter) → `_classify` (`feed_classify`) → `_extract` (`feed_extract`) → `_score` → `lead_confidence`. Imports helpers from `linkedin_agent` — **no shared base class**.
- **New route:** `app/routes/linkedin_feed_routes.py` — `POST /run-linkedin-feed-agent` (async 202), reusing the shared `GET /harvest-status/{job_id}` and `run_guard` single-flight (serialized against Jobs on the same Chrome profile).
- **Shared, unchanged:** same `SessionManager("linkedin")` / `li_at` auth, same `LLMService.extract_json`, same `HarvestRunService` + tables. Feed leads reuse `scraped_jobs` with `source="LinkedIn Feed"` + `lead_confidence`; **no new table**.
- **Frontend:** wired as an async `"feed"` source in `HarvestAgent.jsx`; `stallWatch.js` adds a non-fatal "no progress / LLM may be slow" watchdog during polling.

**Plug points for new feed work:** new agent under `app/agents/`, new route registered in `main.py`, `source` tag + optional column on `scraped_jobs`, and a new `LlmCallType` constant — all reusing the existing browser/session, LLM service, persistence service, and status/polling infrastructure.

---

*Verified against: `linkedin_agent.py`, `linkedin_routes.py`, `run_harvest_agent.py`, `browser_manager.py`, `session_manager.py`, `llm_service.py`, `harvest_run_service.py`, `models/harvest_run.py`, `models/recruiter.py`, `docker-compose.yml`, `nginx.conf`, `HarvestAgent.jsx`, `api.js`.*
