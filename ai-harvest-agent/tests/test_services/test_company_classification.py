"""Company classification: overrides, the "consulting" fix, DB keyword terms and the
LLM check for Ambiguous companies (no real LLM — a fake stands in)."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.company_override import ClassificationKeywordORM, CompanyOverrideORM
from app.models.harvest_config import FiltersConfig, HarvestConfig
from app.models.harvest_run import HarvestRunORM, LlmCallType, ScrapedJobORM
from app.models.unified_job import UnifiedJob
from app.services import company_classification_service as ccs
from app.services.business_filter_service import (
    BusinessFilterService,
    ClassificationContext,
    CompanyOverride,
)

TENANT = "internal"


def _uniq(name: str) -> str:
    """A company name unique to this test (the in-memory DB is shared)."""
    return f"{name} {uuid.uuid4().hex[:8]}"


def _job(company: str, *, title: str = "Data Engineer", desc: str = "Build pipelines.",
         url: str | None = None, **kw) -> UnifiedJob:
    return UnifiedJob(
        job_title=title, company=company, location="Chennai", salary="", experience="",
        posted_date="", job_url=url or f"https://example.com/{uuid.uuid4().hex}",
        job_description=desc, skills=[], work_mode="onsite", source="LinkedIn", **kw,
    )


def _classify(jobs: list[UnifiedJob], ctx: ClassificationContext | None = None) -> list[UnifiedJob]:
    return BusinessFilterService(ctx).classify_all(jobs, FiltersConfig(), track_ambiguous=False)


@pytest.fixture
def llm_on(monkeypatch):
    """Switch the LLM check back on (conftest turns it off) with small caps."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "company_llm_classify_enabled", True)
    monkeypatch.setattr(s, "company_llm_classify_max_companies", 50)
    monkeypatch.setattr(s, "company_llm_classify_batch_size", 2)
    monkeypatch.setattr(s, "company_llm_classify_timeout_s", 5.0)
    return s


class FakeLLM:
    """extract_json fake: answers from ``labels`` (company name → label) and records
    each prompt; ``fail`` raises instead. Mirrors LLMService.get_call_log()."""

    def __init__(self, labels: dict[str, str] | None = None, fail: bool = False) -> None:
        self.labels = labels or {}
        self.fail = fail
        self.prompts: list[str] = []
        self._log: list[dict] = []

    async def extract_json(self, content, schema_description, system="", debug_dir=None,
                           job_url=None, call_type="job_harvest"):
        self.prompts.append(content)
        self._log.append({"provider": "claude", "model": "fake", "prompt": content,
                          "call_type": call_type, "success": not self.fail})
        if self.fail:
            raise RuntimeError("provider down")
        answers = []
        for line in content.splitlines()[1:]:
            num, rest = line.split(". Company: ", 1)
            name = rest.split(" | ", 1)[0]
            answers.append({"id": int(num), "classification": self.labels.get(name, "Unknown")})
        return {"companies": answers}

    def get_call_log(self) -> list[dict]:
        return list(self._log)

    def get_usage_summary(self) -> dict:
        return {"claude": {"calls": len(self._log)}, "total": {"calls": len(self._log)}}


@pytest_asyncio.fixture
async def clean_tables(db_session):
    yield
    await db_session.rollback()
    await db_session.execute(delete(CompanyOverrideORM))
    await db_session.execute(delete(ClassificationKeywordORM))
    await db_session.commit()


# ── Keyword rules: the "consulting" false positive ───────────────────────────

def test_consulting_company_is_no_longer_a_staffing_firm() -> None:
    (j,) = _classify([_job("Acme Data Consulting")])
    assert j.hiring_entity == "Ambiguous"
    assert j.is_gcc is False


def test_real_staffing_words_still_classify_as_staffing() -> None:
    jobs = _classify([_job("Acme Staffing"), _job("Zenith Placement Services"),
                      _job("Bright Outsourcing"), _job("Nova Talent Solutions")])
    assert [j.hiring_entity for j in jobs] == ["Staffing Firm"] * 4


# ── Overrides in classification ──────────────────────────────────────────────

def test_override_beats_keyword_rules_and_existing_value() -> None:
    ctx = ClassificationContext(overrides={
        "acme staffing": CompanyOverride("acme staffing", "Acme Staffing", hiring_entity="Direct Client",
                                         company_size="51-200 employees", domain="Finance"),
    })
    pre = _job("Acme Staffing Pvt Ltd", hiring_entity="Staffing Firm")  # e.g. set earlier in the run
    (j,) = _classify([pre], ctx)
    assert j.hiring_entity == "Direct Client"
    assert j.company_size == "51-200 employees"
    assert j.domain == "Finance"


def test_override_gcc_sets_is_gcc_and_null_fields_fall_back() -> None:
    ctx = ClassificationContext(overrides={
        "foo india": CompanyOverride("foo india", hiring_entity="GCC"),  # key drops "Labs"
        "bar staffing": CompanyOverride("bar staffing", company_size="11-50 employees"),
    })
    gcc, bar = _classify([_job("Foo Labs India"), _job("Bar Staffing")], ctx)
    assert (gcc.hiring_entity, gcc.is_gcc) == ("GCC", True)
    # No hiring_entity in the override → keyword rule still applies.
    assert bar.hiring_entity == "Staffing Firm"
    assert bar.company_size == "11-50 employees"


async def test_db_keywords_apply_without_restart(db_session, clean_tables) -> None:
    name = _uniq("Quirkle")  # an unknown word → Ambiguous with the JSON lists only
    (before,) = _classify([_job(name)], await ccs.load_classification_context(db_session, TENANT))
    assert before.hiring_entity == "Ambiguous"

    word = name.split()[1]  # the unique hex token
    await ccs.add_keyword(db_session, "staffing", ccs.normalize_term(word), tenant_id=TENANT)
    (after,) = _classify([_job(name)], await ccs.load_classification_context(db_session, TENANT))
    assert after.hiring_entity == "Staffing Firm"

    await ccs.add_keyword(db_session, "gcc", "zorblax", tenant_id=TENANT)
    await ccs.add_keyword(db_session, "direct", "plimbo", tenant_id=TENANT)
    ctx = await ccs.load_classification_context(db_session, TENANT)
    gcc, direct = _classify([_job("Zorblax Technologies"), _job("Plimbo")], ctx)
    assert gcc.hiring_entity == "GCC" and gcc.is_gcc
    assert direct.hiring_entity == "Direct Client"


async def test_keywords_are_tenant_scoped(db_session, clean_tables) -> None:
    await ccs.add_keyword(db_session, "staffing", "wumpus", tenant_id="client_us")
    ctx = await ccs.load_classification_context(db_session, TENANT)
    (j,) = _classify([_job("Wumpus Corp")], ctx)
    assert j.hiring_entity == "Ambiguous"


# ── Manual vs llm rows ───────────────────────────────────────────────────────

async def test_llm_write_never_overwrites_manual(db_session, clean_tables) -> None:
    name = _uniq("Hexa Consulting")
    await ccs.upsert_manual_override(db_session, company_name=name,
                                     fields={"hiring_entity": "Direct Client"}, tenant_id=TENANT)
    added = await ccs.save_llm_overrides(db_session, [
        CompanyOverride(ccs.normalize_company_key(name), name, hiring_entity="Staffing Firm", source="llm"),
    ], tenant_id=TENANT)
    assert added == 0
    ctx = await ccs.load_classification_context(db_session, TENANT)
    (j,) = _classify([_job(name)], ctx)
    assert j.hiring_entity == "Direct Client"
    assert ctx.override_for(name).source == "manual"


async def test_manual_write_replaces_llm_row(db_session, clean_tables) -> None:
    name = _uniq("Orbit Consulting")
    key = ccs.normalize_company_key(name)
    await ccs.save_llm_overrides(db_session, [CompanyOverride(key, name, hiring_entity="Staffing Firm",
                                                              source="llm")], tenant_id=TENANT)
    ctx = await ccs.load_classification_context(db_session, TENANT)
    (j,) = _classify([_job(name)], ctx)
    assert j.hiring_entity == "Staffing Firm"  # llm beats keywords (which say Ambiguous)

    row = await ccs.upsert_manual_override(db_session, company_name=name,
                                           fields={"company_size": "51-200 employees"},
                                           email="lead@x.com", tenant_id=TENANT)
    assert row.source == "manual"
    assert row.hiring_entity == "Staffing Firm"  # kept — the editor saw it and didn't change it
    row = await ccs.upsert_manual_override(db_session, company_name=name,
                                           fields={"hiring_entity": "GCC"}, tenant_id=TENANT)
    ctx = await ccs.load_classification_context(db_session, TENANT)
    (j,) = _classify([_job(name)], ctx)
    assert (j.hiring_entity, j.company_size) == ("GCC", "51-200 employees")


# ── LLM check ────────────────────────────────────────────────────────────────

async def test_llm_check_batches_caches_and_applies(llm_on) -> None:
    names = ["Alpha Consulting", "Beta Works", "Gamma Analytics"]
    jobs = _classify([_job(n) for n in names] + [_job("Alpha Consulting"), _job("Acme Staffing")])
    assert [j.hiring_entity for j in jobs[:4]] == ["Ambiguous"] * 4
    ctx = ClassificationContext(overrides={
        # Already asked once ("Unknown") → not asked again.
        "gamma analytics": CompanyOverride("gamma analytics", "Gamma Analytics", source="llm"),
    })
    llm = FakeLLM({"Alpha Consulting": "Staffing Firm", "Beta Works": "Unknown"})
    outcome = await ccs.llm_classify_ambiguous(jobs, ctx, llm=llm)

    assert len(llm.prompts) == 1             # 2 candidates, batch size 2 → one call
    assert "Gamma Analytics" not in llm.prompts[0] and "Acme Staffing" not in llm.prompts[0]
    assert outcome.asked == 2 and outcome.classified == 1
    by_key = {o.company_key: o for o in outcome.overrides}
    assert by_key["alpha consulting"].hiring_entity == "Staffing Firm"
    assert by_key["alpha consulting"].source == "llm"
    assert by_key["beta works"].hiring_entity is None    # Unknown: cached, stays Ambiguous
    assert [j.hiring_entity for j in jobs] == [
        "Staffing Firm", "Ambiguous", "Ambiguous", "Staffing Firm", "Staffing Firm"]
    assert outcome.jobs_updated == 2
    assert outcome.call_log and outcome.call_log[0]["call_type"] == LlmCallType.COMPANY_CLASSIFY
    assert "alpha consulting" in ctx.overrides  # context updated for the rest of the run


async def test_llm_check_respects_batch_size_and_cap(llm_on, monkeypatch) -> None:
    monkeypatch.setattr(llm_on, "company_llm_classify_max_companies", 5)
    jobs = _classify([_job(f"Zyx Unknown {i}") for i in range(8)])
    llm = FakeLLM()
    outcome = await ccs.llm_classify_ambiguous(jobs, ClassificationContext(), llm=llm)
    assert outcome.asked == 5
    assert len(llm.prompts) == 3   # 2 + 2 + 1


async def test_llm_check_failure_never_raises(llm_on) -> None:
    jobs = _classify([_job("Delta Consulting"), _job("Epsilon Works")])
    outcome = await ccs.llm_classify_ambiguous(jobs, ClassificationContext(), llm=FakeLLM(fail=True))
    assert outcome.overrides == []          # nothing cached → asked again next run
    assert [j.hiring_entity for j in jobs] == ["Ambiguous", "Ambiguous"]
    assert outcome.call_log and outcome.call_log[0]["success"] is False


async def test_llm_check_skipped_when_disabled_or_unconfigured(llm_on, monkeypatch) -> None:
    jobs = _classify([_job("Kappa Consulting")])
    llm = FakeLLM({"Kappa Consulting": "GCC"})
    monkeypatch.setattr(llm_on, "company_llm_classify_enabled", False)
    out = await ccs.llm_classify_ambiguous(jobs, ClassificationContext(), llm=llm)
    assert out.asked == 0 and not llm.prompts

    monkeypatch.setattr(llm_on, "company_llm_classify_enabled", True)
    import app.services.jd_llm_enrichment as jd
    monkeypatch.setattr(jd, "llm_configured", lambda _s: False)
    out = await ccs.llm_classify_ambiguous(jobs, ClassificationContext())  # no llm given
    assert out.asked == 0 and jobs[0].hiring_entity == "Ambiguous"


async def test_llm_check_garbage_answer_is_ignored(llm_on) -> None:
    class Garbage(FakeLLM):
        async def extract_json(self, *a, **kw):
            await super().extract_json(*a, **kw)
            return {"companies": [{"id": "x"}, {"id": 1, "classification": "Maybe"}, "bad"]}

    jobs = _classify([_job("Lambda Consulting")])
    outcome = await ccs.llm_classify_ambiguous(jobs, ClassificationContext(), llm=Garbage())
    assert outcome.overrides == [] and jobs[0].hiring_entity == "Ambiguous"


async def test_orchestrator_applies_context_and_llm(llm_on, monkeypatch) -> None:
    """run_all: override wins in step 3, the LLM labels the remaining Ambiguous
    company, its call is added to llm_calls and its verdict returned for caching."""
    import app.agents.orchestrator_agent as orch_mod
    import app.services.company_classification_service as svc_mod
    from app.services.llm_service import empty_usage_summary

    raw = {"LinkedIn": [_job("Omega Consulting"), _job("Sigma Staffing")]}

    async def fake_collect(self, config, **kw):
        return raw, empty_usage_summary(), [], []

    monkeypatch.setattr(orch_mod.OrchestratorAgent, "_collect_all", fake_collect)
    fake = FakeLLM({"Omega Consulting": "Direct Client"})
    real = svc_mod.llm_classify_ambiguous

    async def with_fake(jobs, ctx, **kw):
        return await real(jobs, ctx, llm=fake)

    monkeypatch.setattr(svc_mod, "llm_classify_ambiguous", with_fake)
    ctx = ClassificationContext(overrides={
        "sigma staffing": CompanyOverride("sigma staffing", hiring_entity="GCC"),
    })
    result = await orch_mod.OrchestratorAgent(HarvestConfig(), classification_context=ctx).run_all()
    by_company = {j.company: j.hiring_entity for j in result.all_jobs}
    assert by_company == {"Omega Consulting": "Direct Client", "Sigma Staffing": "GCC"}
    assert [o.company_key for o in result.company_llm_overrides] == ["omega consulting"]
    assert any(c.get("call_type") == LlmCallType.COMPANY_CLASSIFY for c in result.llm_calls)


async def test_orchestrator_without_context_skips_llm(llm_on, monkeypatch) -> None:
    import app.agents.orchestrator_agent as orch_mod
    import app.services.company_classification_service as svc_mod
    from app.services.llm_service import empty_usage_summary

    async def fake_collect(self, config, **kw):
        return {"LinkedIn": [_job("Omicron Consulting")]}, empty_usage_summary(), [], []

    async def boom(*a, **kw):
        raise AssertionError("LLM check must not run without a context")

    monkeypatch.setattr(orch_mod.OrchestratorAgent, "_collect_all", fake_collect)
    monkeypatch.setattr(svc_mod, "llm_classify_ambiguous", boom)
    result = await orch_mod.OrchestratorAgent(HarvestConfig()).run_all()
    assert result.all_jobs[0].hiring_entity == "Ambiguous"
    assert result.company_llm_overrides == []


# ── Insert time (HarvestRunService.bulk_insert_scraped_jobs) ─────────────────

async def test_bulk_insert_applies_overrides(db_session, clean_tables) -> None:
    from app.services.harvest_run_service import HarvestRunService

    manual, llm_co, plain = _uniq("Mancorp"), _uniq("Llmcorp"), _uniq("Plaincorp")
    await ccs.upsert_manual_override(db_session, company_name=manual, tenant_id=TENANT, fields={
        "hiring_entity": "Direct Client", "company_size": "1001-5000 employees", "domain": "Finance"})
    await ccs.save_llm_overrides(db_session, [
        CompanyOverride(ccs.normalize_company_key(llm_co), llm_co, hiring_entity="GCC", source="llm"),
    ], tenant_id=TENANT)
    run = HarvestRunORM(run_id=f"ccs-{uuid.uuid4().hex[:10]}")
    db_session.add(run)
    await db_session.flush()
    await HarvestRunService(db_session).bulk_insert_scraped_jobs(run.id, [
        # LinkedIn Feed style: the feed's own LLM label + a scraped size.
        {"source": "LinkedIn Feed", "company": manual, "job_url": "u1",
         "hiring_entity": "Staffing Firm", "company_size": "11-50 employees"},
        {"source": "LinkedIn", "company": llm_co, "job_url": "u2", "hiring_entity": "Ambiguous"},
        {"source": "LinkedIn", "company": llm_co, "job_url": "u3", "hiring_entity": "Staffing Firm"},
        {"source": "LinkedIn", "company": plain, "job_url": "u4", "hiring_entity": "Ambiguous"},
    ], auto_assign=False)
    rows = (await db_session.execute(
        select(ScrapedJobORM).where(ScrapedJobORM.run_id == run.id).order_by(ScrapedJobORM.job_url)
    )).scalars().all()
    got = [(r.hiring_entity, r.is_gcc, r.company_size, r.domain) for r in rows]
    assert got[0] == ("Direct Client", False, "1001-5000 employees", "Finance")  # manual wins
    assert got[1][:2] == ("GCC", True)            # llm fills Ambiguous
    assert got[2][:2] == ("Staffing Firm", False)  # llm never replaces a real label
    assert got[3][:2] == ("Ambiguous", False)
