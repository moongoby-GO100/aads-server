"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, CollectorJob, CollectorOverview, CollectorSite } from "@/lib/api";

const PROJECTS = ["ALL", "AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CUSTOM"];
const statusLabel: Record<string, string> = {queued: "대기", running: "실행 중", action_required: "개입 필요", succeeded: "완료", failed: "실패", superseded: "대체됨"};
const runtimeLabel: Record<string, string> = {webview2: "Windows 앱", chrome_extension: "Chrome 확장", chrome_cdp: "Chrome 연결", playwright_server: "서버 브라우저", file_upload: "파일 업로드", official_api: "공식 API", manual_export: "수동 내보내기"};

export default function AuthenticatedCollectorPage() {
  const [project, setProject] = useState("ALL");
  const [overview, setOverview] = useState<CollectorOverview | null>(null);
  const [sites, setSites] = useState<CollectorSite[]>([]);
  const [jobs, setJobs] = useState<CollectorJob[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      setError("");
      const key = project === "ALL" ? undefined : project;
      const [summary, siteResult, jobResult] = await Promise.all([
        api.getCollectorOverview(), api.getCollectorSites(key), api.getCollectorJobs(key),
      ]);
      setOverview(summary); setSites(siteResult.sites); setJobs(jobResult.jobs);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "수집 현황을 불러오지 못했습니다.");
    }
  }, [project]);

  useEffect(() => { void load(); }, [load]);

  const actionJobs = useMemo(() => jobs.filter(job => job.status === "action_required"), [jobs]);
  const resume = async (job: CollectorJob) => {
    setBusy(job.id);
    try { await api.resumeCollectorJob(job.id, "completed"); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "작업을 재개하지 못했습니다."); }
    finally { setBusy(""); }
  };
  const totals = overview?.totals;

  return (
    <main className="min-h-screen p-4 md:p-8" style={{background: "var(--bg-primary)", color: "var(--text-primary)"}}>
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div><p className="text-sm" style={{color: "var(--accent)"}}>Authenticated Site Collector</p><h1 className="text-2xl font-bold">로그인 사이트 수집 허브</h1><p className="mt-1 text-sm" style={{color: "var(--text-secondary)"}}>프로젝트별 로그인 세션, 레시피, 수집 작업을 한 곳에서 관리합니다.</p></div>
          <div className="flex flex-wrap gap-2"><button className="rounded-lg border px-4 py-2 text-sm">새 사이트 연결</button><button className="rounded-lg px-4 py-2 text-sm text-white" style={{background: "var(--accent)"}}>새 수집 작업</button></div>
        </header>

        <section className="flex gap-2 overflow-x-auto pb-1" aria-label="프로젝트 필터">
          {PROJECTS.map(key => <button key={key} onClick={() => setProject(key)} className="shrink-0 rounded-full px-4 py-2 text-sm" style={{background: project === key ? "var(--accent)" : "var(--bg-card)", color: project === key ? "white" : "var(--text-secondary)"}}>{key === "ALL" ? "전체 프로젝트" : key}</button>)}
        </section>

        {error && <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-300">{error} <button className="ml-2 underline" onClick={() => void load()}>다시 시도</button></div>}

        <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
          {[["연결된 사이트", totals?.connected_sites], ["활성 계정", totals?.active_accounts], ["실행 중", totals?.running_jobs], ["개입 필요", totals?.action_required_jobs], ["최근 실패", totals?.failed_jobs]].map(([label, value]) => <article key={String(label)} className="rounded-xl p-4" style={{background: "var(--bg-card)"}}><p className="text-xs" style={{color: "var(--text-secondary)"}}>{label}</p><p className="mt-2 text-2xl font-bold">{value ?? "—"}</p></article>)}
        </section>

        {actionJobs.length > 0 && <section className="rounded-2xl border border-amber-500/40 bg-amber-500/5 p-4 md:p-6"><h2 className="font-semibold text-amber-300">사용자 개입 필요</h2><div className="mt-3 space-y-3">{actionJobs.map(job => <div key={job.id} className="flex flex-col gap-3 rounded-xl p-4 md:flex-row md:items-center md:justify-between" style={{background: "var(--bg-card)"}}><div><p className="font-medium">{job.payload.project_key} · {job.site_key}</p><p className="text-sm text-amber-200">{job.error_code || "로그인 확인 필요"} — {job.message || "OTP/CAPTCHA/약관/권한 상태를 직접 확인해 주세요."}</p><p className="mt-1 text-xs" style={{color: "var(--text-secondary)"}}>자동 우회하지 않으며 현재 세션에서 안전하게 이어집니다.</p></div><button disabled={busy === job.id} onClick={() => void resume(job)} className="rounded-lg bg-amber-400 px-4 py-2 text-sm font-semibold text-black disabled:opacity-50">{busy === job.id ? "재개 중…" : "조치 완료 후 재개"}</button></div>)}</div></section>}

        <section><div className="mb-3 flex items-center justify-between"><h2 className="text-lg font-semibold">사이트 연결</h2><button className="text-sm" style={{color: "var(--accent)"}}>설정 및 레시피 관리</button></div><div className="grid gap-3 lg:grid-cols-2">{sites.map(site => <article key={site.id} className="rounded-xl p-4" style={{background: "var(--bg-card)"}}><div className="flex items-start justify-between"><div><p className="text-xs" style={{color: "var(--text-secondary)"}}>{site.project_key}</p><h3 className="font-semibold">{site.display_name}</h3></div><span className="rounded-full px-2 py-1 text-xs" style={{background: site.connected_account_count ? "rgba(34,197,94,.15)" : "rgba(245,158,11,.15)", color: site.connected_account_count ? "#86efac" : "#fcd34d"}}>{site.connected_account_count ? "로그인 연결됨" : "로그인 필요"}</span></div><div className="mt-4 flex flex-wrap gap-2 text-xs"><span className="rounded bg-black/20 px-2 py-1">{runtimeLabel[site.runtime] || site.runtime}</span>{site.data_categories.map(category => <span key={category} className="rounded bg-black/20 px-2 py-1">{category}</span>)}</div><p className="mt-3 text-xs" style={{color: "var(--text-secondary)"}}>마지막 수집: {site.last_collected_at ? new Date(site.last_collected_at).toLocaleString("ko-KR") : "수집 이력 없음"}</p></article>)}{!sites.length && !error && <div className="rounded-xl border border-dashed p-8 text-center text-sm" style={{color: "var(--text-secondary)"}}>연결된 사이트가 없습니다. 첫 사이트를 연결해 주세요.</div>}</div></section>

        <section><h2 className="mb-3 text-lg font-semibold">작업 큐</h2><div className="overflow-x-auto rounded-xl" style={{background: "var(--bg-card)"}}><table className="w-full min-w-[680px] text-left text-sm"><thead style={{color: "var(--text-secondary)"}}><tr><th className="p-4">프로젝트</th><th>사이트</th><th>런타임</th><th>상태</th><th>마지막 갱신</th><th>다음 행동</th></tr></thead><tbody>{jobs.map(job => <tr key={job.id} className="border-t" style={{borderColor: "var(--border)"}}><td className="p-4">{job.payload.project_key || "CUSTOM"}</td><td>{job.site_key}</td><td>{runtimeLabel[job.runtime] || job.runtime}</td><td>{statusLabel[job.status] || job.status}</td><td>{new Date(job.updated_at).toLocaleString("ko-KR")}</td><td>{job.status === "failed" ? "다시 로그인 · 수동 업로드 · 담당자 승인" : job.status === "action_required" ? "사용자 확인 후 재개" : "—"}</td></tr>)}</tbody></table></div></section>
      </div>
    </main>
  );
}
