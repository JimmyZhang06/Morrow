import { useEffect, useRef, useState } from "react";
import type { ApiSettings } from "./types";
import {
  localSearchStatus, setLocalSearchPermission, searchDiaryHistory, rebuildLocalSearch,
  safeApiMessage, type LocalSearchStatus, type LocalSearchResult,
  getDiaryIndexJob, startDiaryIndexJob, cancelDiaryIndexJob, type DiaryIndexJob,
} from "./api";

export function LocalSearchPanel({ settings, revisionKey, unsyncedCount, background, onSelect }: {
  settings: ApiSettings; revisionKey: string; unsyncedCount: number; onSelect: (id: string) => void;
  background: boolean;
}) {
  const [status, setStatus] = useState<LocalSearchStatus | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<LocalSearchResult | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const [job, setJob] = useState<DiaryIndexJob | null>(null);
  useEffect(() => {
    if (!background) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const response = await getDiaryIndexJob(settings);
      if (!active) return;
      if (response.ok) setJob(response.data);
      timer = setTimeout(() => void poll(), 1500);
    }
    setJob(null);
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [settings, background]);

  async function manageJob(cancel = false) {
    if (busy) return;
    const current = generation.current;
    setBusy(true); setMessage("");
    try {
      const response = cancel && job ? await cancelDiaryIndexJob(settings, job.id)
        : await startDiaryIndexJob(settings);
      if (current !== generation.current) return;
      if (!response.ok) setMessage(safeApiMessage(response, "索引任务未更新"));
      else {
        const latest = await getDiaryIndexJob(settings);
        if (current === generation.current && latest.ok) setJob(latest.data);
      }
    } finally { if (current === generation.current) setBusy(false); }
  }
  useEffect(() => {
    const current = ++generation.current;
    setResults(null); setBusy(false); setStatus(null); setMessage("");
    void localSearchStatus(settings).then((response) => {
      if (current !== generation.current) return;
      if (response.ok) setStatus(response.data);
      else setMessage(safeApiMessage(response, "无法读取搜索设置"));
    });
    return () => { generation.current += 1; };
  }, [settings, revisionKey]);

  async function perform(operation: "permission" | "search" | "rebuild") {
    if (!status || busy) return;
    const current = generation.current;
    setBusy(true); setMessage(""); setResults(null);
    try {
      if (operation === "permission") {
        const response = await setLocalSearchPermission(settings, !status.enabled, status.policy_epoch);
        if (current !== generation.current) return;
        if (!response.ok) {
          setMessage(safeApiMessage(response, "设置未保存"));
          const refreshed = await localSearchStatus(settings);
          if (current === generation.current && refreshed.ok) setStatus(refreshed.data);
        } else {
          setStatus(response.data);
          setMessage(response.data.enabled ? "已开启。新记录自动建立本地索引；旧记录可以补建索引。" : "已关闭，搜索索引已清除，原日记保留。");
        }
      } else {
        const response = operation === "search"
          ? await searchDiaryHistory(settings, query.trim()) : await rebuildLocalSearch(settings);
        if (current !== generation.current) return;
        if (response.ok) {
          setResults(response.data);
          if (operation === "rebuild") setMessage(`本批处理 ${response.data.rebuilt} 条，当前范围内还有 ${response.data.pending} 条待处理。`);
        } else setMessage(safeApiMessage(response, "本地搜索未完成"));
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  }

  return <details className="local-search-panel" aria-label="日记历史搜索"><summary>搜索更多历史<span>{status?.enabled ? "本地搜索已开启" : "可选 · 在本机搜索日记"}</span></summary><div className="local-search-content">
    <div className="local-search-heading"><strong>搜索过去的日记</strong>
      {status && <button className="button secondary" disabled={busy} onClick={() => void perform("permission")}>
        {status.enabled ? "关闭本地搜索" : "开启本地搜索"}
      </button>}
    </div>
    <p>在本机建立可读取的关键词索引，不会发送给在线模型。高度敏感记录和对话不进入此索引。</p>
    {unsyncedCount > 0 && <p role="status">有 {unsyncedCount} 条记录尚未同步到本机数据库，暂不在搜索范围内。可通过下方记录列表中的“尝试同步”补齐。</p>}
    {status?.enabled && <form onSubmit={(event) => { event.preventDefault(); void perform("search"); }}>
      <input aria-label="搜索日记关键词" value={query} maxLength={500}
        onChange={(event) => setQuery(event.target.value)} placeholder="例如：项目、工作、散步" />
      <button className="button primary" disabled={busy || !query.trim()}>{busy ? "处理中…" : "搜索"}</button>
      <button className="button secondary" type="button" disabled={busy || job?.state === "queued"}
        onClick={() => void (background ? manageJob() : perform("rebuild"))}>补建索引</button>
    </form>}
    {message && <p role="status">{message}</p>}
    {status?.enabled && background && job && <div aria-live="polite">
      <p>{job.state === "queued" ? "正在后台补建，可以离开此页面，重启后会继续。" : job.state === "completed" ? "本轮补建完成。" : job.state === "canceled" ? "补建已取消，已建立的索引保留。" : "补建未完成，可重新开始。"}
        已检查 {job.processed} 条，建立索引 {job.indexed} 条。</p>
      {job.state === "queued" && <button className="button secondary" disabled={busy} onClick={() => void manageJob(true)}>取消补建</button>}
    </div>}
    {results && <div aria-live="polite">
      <p>扫描 {results.scanned} 条片段，已索引 {results.indexed} 条。{results.truncated ? "当前仅覆盖最近 1000 条片段，结果不代表全部历史。" : ""}</p>
      {results.items.map((hit) => <button type="button" className="local-search-hit"
        key={hit.fragment_id} onClick={() => onSelect(hit.entry_id)}>{hit.excerpt}<span>打开原记录 →</span></button>)}
      {!results.items.length && query.trim() && <p>本次没有搜索结果。可以换一个关键词，或补建旧记录的索引。</p>}
    </div>}
  </div></details>;
}
