import { useEffect, useState } from "react";
import {
  Routes, Route, Navigate, Outlet, useNavigate, useLocation, useParams,
} from "react-router-dom";
import JobDetailsView from "./JobDetailsView";
import RuleEngineConfig from "./RuleEngineConfig";
import OutreachHistoryPage from "./OutreachHistoryPage";
import OutreachThreadPage from "./OutreachThreadPage";
import Sidebar from "./components/Sidebar";
import ThemeStyles from "./components/ThemeStyles";
import IntroSplash from "./components/IntroSplash";
import { getJob } from "./api";
import { mapApiJob, mapJobToDetail } from "./lib/jobsData";
import { HarvestDataProvider, useHarvestData } from "./HarvestDataContext";
import { useTenant } from "./TenantContext";
import JobsPage from "./pages/JobsPage";
import RunHistoryPage from "./pages/RunHistoryPage";
import RunDetailView from "./pages/RunDetailView";
import SourceRunsPage from "./pages/SourcesPage";
import LeadIntelligencePage from "./pages/LeadIntelligencePage";
import RuleEngineRedesign from "./pages/RuleEngineRedesign";

/* Per-tenant route guard: a page whose feature flag is off for the current
 * tenant redirects to /jobs. Presentation-level only — the backend enforces
 * data access independently (RLS + tenant-scoped queries). */
function FeatureGate({ feature, children }) {
  const { hasFeature } = useTenant();
  if (!hasFeature(feature)) return <Navigate to="/jobs" replace />;
  return children;
}

// Map the current pathname to the Sidebar's active nav key.
function activeKeyFromPath(pathname) {
  if (pathname.startsWith("/rules")) return "rules";
  if (pathname.startsWith("/history")) return "history";
  if (pathname.startsWith("/sources")) return "sources";
  if (pathname.startsWith("/leads")) return "leads";
  if (pathname.startsWith("/outreach") || pathname.startsWith("/mail")) return "outreach";
  return "jobs"; // "/", "/jobs", "/jobs/:id"
}

/* Shared layout for the sidebar-chrome pages (jobs/history/sources/leads/rules/
 * outreach): renders the single Sidebar + the routed page via <Outlet/>. The
 * full-page detail views (JobDetailsView, RunDetailView) render their own root
 * and sit OUTSIDE this layout, exactly as before. */
function AppLayout({ onLogout }) {
  const { jobsTotal, runs } = useHarvestData();
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <div className="ha-root">
      <ThemeStyles />
      <Sidebar
        activePage={activeKeyFromPath(location.pathname)}
        onNavigate={(key) => navigate(`/${key}`)}
        jobsCount={jobsTotal}
        runsCount={runs.length}
        onLogout={onLogout}
      />
      <Outlet />
    </div>
  );
}

// ── Route wrappers: read shared context + wire router navigation into the
// (otherwise unchanged) page components. onView/onNavigate become route pushes.
function JobsRoute() {
  const { pageSizeSel, setPageSizeSel } = useHarvestData();
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <JobsPage
      onNavigate={(key) => navigate(`/${key}`)}
      // Carry the current jobs URL (with its ?company=…&page=… query) as `from`, so
      // the detail view's Back returns to the exact filtered/paged list instead of
      // a bare /jobs that resets everything.
      onView={(dv) => navigate(`/jobs/${encodeURIComponent(dv.job.id)}`, { state: { job: dv.job, from: location.pathname + location.search } })}
      pageSizeSel={pageSizeSel} onPageSizeChange={setPageSizeSel}
      urlState
    />
  );
}

function JobDetailRoute() {
  const { jobId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  // Prefer the object passed via navigation state (instant); on a refresh/direct
  // link there's no in-memory list anymore (the jobs page is server-paginated),
  // so fetch the single record from GET /jobs/{id}.
  const stateJob = location.state?.job;
  const [fetchedJob, setFetchedJob] = useState(null);
  const [loadState, setLoadState] = useState(stateJob ? "done" : "loading");

  useEffect(() => {
    if (stateJob) return undefined;
    let cancelled = false;
    setLoadState("loading");
    setFetchedJob(null);
    (async () => {
      try {
        const res = await getJob(jobId);
        if (!cancelled) {
          setFetchedJob(mapJobToDetail(mapApiJob(res)));
          setLoadState("done");
        }
      } catch {
        if (!cancelled) setLoadState("error"); // 404 or backend unreachable
      }
    })();
    return () => { cancelled = true; };
  }, [jobId, stateJob]);

  const job = stateJob || fetchedJob;

  if (!job) {
    // Rendered outside the shared layout (no ThemeStyles here), so keep it
    // self-contained with inline styles.
    return (
      <div style={{ minHeight: "100vh", background: "#F8FAFC", padding: 40, color: "#64748B", fontFamily: 'ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif' }}>
        {loadState === "loading" ? "Loading job…" : (
          <div>
            <div style={{ marginBottom: 12 }}>This job isn’t in the current list.</div>
            <button
              onClick={() => navigate("/jobs")}
              style={{ background: "#fff", border: "1px solid #CBD5E1", color: "#2563EB", borderRadius: 8, padding: "8px 16px", fontSize: 14, cursor: "pointer" }}
            >
              Back to Harvested Jobs
            </button>
          </div>
        )}
      </div>
    );
  }
  // Detail views are full-page (own root, no sidebar) — render outside the layout
  // chrome by returning them directly from a route mounted above AppLayout.
  return <JobDetailsView job={job} onBack={() => navigate(location.state?.from || "/jobs")} />;
}

function HistoryRoute() {
  const { runs, runsLoading, runsError, fetchRuns } = useHarvestData();
  const navigate = useNavigate();
  return (
    <RunHistoryPage
      runs={runs} loading={runsLoading} error={runsError} onRefresh={fetchRuns}
      onNavigate={(key) => navigate(`/${key}`)}
      onView={(runId) => navigate(`/history/${encodeURIComponent(runId)}`)}
    />
  );
}

function RunDetailRoute() {
  const { runId } = useParams();
  const navigate = useNavigate();
  return (
    <RunDetailView
      runId={runId}
      onBack={() => navigate("/history")}
      onView={(dv) => navigate(`/jobs/${encodeURIComponent(dv.job.id)}`, { state: { job: dv.job } })}
    />
  );
}

function SourcesRoute() {
  const { harvestRunning, setHarvestRunning } = useHarvestData();
  return <SourceRunsPage harvestRunning={harvestRunning} setHarvestRunning={setHarvestRunning} />;
}

function RulesRoute() {
  const { jobsTotal, runs, refreshAll, harvestRunning, setHarvestRunning } = useHarvestData();
  const { features } = useTenant();
  const navigate = useNavigate();
  // US client gets the redesigned Rule Engine (features.ruleEngineRedesign);
  // internal keeps the classic RuleEngineConfig unchanged.
  if (features.ruleEngineRedesign === true) return <RuleEngineRedesign />;
  return (
    <RuleEngineConfig
      onNavigate={(key) => navigate(`/${key}`)} jobsCount={jobsTotal} runsCount={runs.length}
      onRunComplete={refreshAll} harvestRunning={harvestRunning} setHarvestRunning={setHarvestRunning}
    />
  );
}

function NotFound() {
  const navigate = useNavigate();
  return (
    <main className="ha-main">
      <div style={{ padding: "64px 40px", textAlign: "center", color: "#64748B" }}>
        <div style={{ fontSize: 40, fontWeight: 800, color: "#1E293B" }}>404</div>
        <div style={{ marginTop: 8, marginBottom: 18 }}>That page doesn’t exist.</div>
        <button className="ha-btn ha-btn-secondary" onClick={() => navigate("/jobs")}>Go to Harvested Jobs</button>
      </div>
    </main>
  );
}

/* Page — owns the shared datasets/effects, provides them via context, and maps
 * URLs to pages via React Router. Auth gating stays in App.js (this whole tree
 * only mounts once authenticated), and the <BrowserRouter> lives there too. */
export default function HarvestAgent({ onLogout }) {
  return (
    <HarvestDataProvider>
      {/* One-time post-login intro splash — dissolves into the app (Harvested
          Jobs) rendering underneath. Plays once per login; skips on reduced motion. */}
      <IntroSplash />
      <Routes>
        {/* Full-page detail views — own root/chrome, no shared sidebar (as before). */}
        <Route path="/jobs/:jobId" element={<JobDetailRoute />} />
        <Route path="/history/:runId" element={<RunDetailRoute />} />

        {/* Sidebar-chrome pages share one layout via <Outlet/>. Post-login the
            home ("/") lands straight on Harvested Jobs. Pages beyond a client
            tenant's feature set redirect to /jobs (FeatureGate). */}
        <Route element={<AppLayout onLogout={onLogout} />}>
          <Route index element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<JobsRoute />} />
          <Route path="/history" element={<FeatureGate feature="history"><HistoryRoute /></FeatureGate>} />
          <Route path="/sources" element={<FeatureGate feature="sources"><SourcesRoute /></FeatureGate>} />
          <Route path="/leads" element={<FeatureGate feature="leads"><LeadIntelligencePage /></FeatureGate>} />
          <Route path="/rules" element={<FeatureGate feature="rules"><RulesRoute /></FeatureGate>} />
          <Route path="/outreach" element={<FeatureGate feature="outreach"><OutreachHistoryPage /></FeatureGate>} />
          <Route path="/mail/:id" element={<FeatureGate feature="outreach"><OutreachThreadPage /></FeatureGate>} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </HarvestDataProvider>
  );
}
