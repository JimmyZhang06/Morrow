import { AppSelect } from "./Picker";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Mail, X, ArrowUpRight, Leaf } from "lucide-react";
import { careAction, checkCare, getCare, saveCarePreferences, safeApiMessage, type CareState } from "./api";
import type { ApiSettings } from "./types";
import "./CareLetters.css";
import "./ConversationRefinement.css";

export function CareLetters({ settings, available, home, masked }: {
  settings: ApiSettings; available: boolean; home: boolean; masked: boolean;
}) {
  const [state, setState] = useState<CareState | null>(null);
  const [reload, setReload] = useState(0);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [later, setLater] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const generation = useRef(0);
  useEffect(() => {
    const epoch = ++generation.current;
    setState(null); setError(""); setBusy(false);
    if (!available) return;
    let timer: ReturnType<typeof setTimeout>;
    let nextCheck = Date.now() + 60000;
    async function refresh() {
      if (document.visibilityState === "visible") {
        const response = await getCare(settings);
        if (generation.current !== epoch) return;
        if (response.ok) {
          setState(response.data); setError("");
          const hour = new Date().getHours();
          const typing = ["TEXTAREA", "INPUT"].includes(document.activeElement?.tagName ?? "");
          if (response.data.enabled && !typing && hour >= 8 && hour < 22 && Date.now() >= nextCheck) {
            nextCheck = Date.now() + 60000;
            await checkCare(settings);
          }
        } else setError(safeApiMessage(response, "信箱暂时未能加载，请重试。"));
      }
      if (generation.current === epoch) timer = setTimeout(() => void refresh(), 15000);
    }
    void refresh();
    return () => { generation.current++; clearTimeout(timer); };
  }, [available, settings, reload]);
  useEffect(() => {
    if (open && !masked) dialog.current?.showModal(); else dialog.current?.close();
  }, [open, masked]);
  async function update(action: "preferences" | "dismiss" | "pause" | "resume", enabled?: boolean, presentation?: "gentle" | "inbox", includePrivate?: boolean) {
    if (!state || busy) return;
    const epoch = generation.current;
    setBusy(true); setError("");
    if (action === "preferences") setState({...state, enabled: enabled ?? state.enabled, presentation: presentation ?? state.presentation});
    try {
      const result = action === "preferences"
        ? await saveCarePreferences(settings, enabled ?? state.enabled, presentation ?? state.presentation, includePrivate ?? state.include_private_diaries ?? false)
        : await careAction(settings, action, state.letter?.id);
      if (generation.current !== epoch) return;
      if (!result.ok) { setState(state); setError(safeApiMessage(result, "未能保存，请稍后重试")); return; }
      const refreshed = await getCare(settings);
      if (generation.current === epoch && refreshed.ok) setState(refreshed.data);
    } finally { if (generation.current === epoch) setBusy(false); }
  }
  if (!available) return null;
  const paused = state?.paused_until && Date.parse(state.paused_until) > Date.now();
  const hour = new Date().getHours();
  const nudge = state?.letter && state.presentation === "gentle" && home && !open && !masked && !paused
    && later !== state.letter.id && hour >= 8 && hour < 22;
  return <>
    <button className="care-launch" onClick={() => setOpen(true)} aria-label={state?.letter ? "来信，有一封待读信" : "关怀来信"}>
      <Mail size={17} /><span>来信</span>{state?.letter && !masked && <i aria-hidden="true" />}
    </button>
    {nudge && <div className="care-note">
      <Leaf size={18} /><div><span>有一封信，留给你</span><small>有空的时候，再慢慢看。</small></div>
      <button onClick={() => setOpen(true)} aria-label="读这封来信"><ArrowUpRight size={18} /></button>
      <button onClick={() => setLater(state!.letter!.id)} aria-label="稍后再看"><X size={15} /></button>
    </div>}
    {createPortal(<dialog ref={dialog} className="care-dialog" onClose={() => setOpen(false)} onCancel={() => setOpen(false)}>
      <header><span><Mail size={18} /> 关怀来信</span><button onClick={() => setOpen(false)} aria-label="关闭来信"><X size={20} /></button></header>
      <div className="care-scroll">
        {error && <p className="care-error" role="alert">{error}</p>}
        {!state ? error ? <button onClick={() => setReload(value => value + 1)}>重新打开信箱</button> : <p role="status">正在打开信箱…</p> : <>
          {state.letter && !paused ? <article className="care-paper">
            <div className="care-dateline">MORROW · {new Date(state.letter.created_at).toLocaleDateString()}</div>
            <h2>给此刻的你</h2>
            {state.letter.body.split(/\n+/).filter(Boolean).map((paragraph, index) => <p key={index}>{paragraph}</p>)}
            <footer>不必急着回应。<span>— Morrow</span></footer>
            <button className="care-done" disabled={busy} onClick={() => void update("dismiss")}>读完了，收起这封信</button>
          </article> : <div className="care-empty"><Leaf size={28} /><h2>{paused ? "给你留一些安静的时间" : state.enabled ? "信箱暂时是空的" : "偶尔，有一封给你的信"}</h2>
            <p>{paused ? "来信已暂停七天，你也可以随时恢复。" : state.enabled ? "当最近的记录或聊天里有值得关心的事情时，Morrow 会试着写来。不需要每天都有一封信。" : "如果你愿意，Morrow 可以在你最近的记录或聊天里留意值得关心的事情，偶尔写一封短笺。你决定什么时候读。"}</p></div>}
          <details className="care-preferences" open={!state.enabled} aria-label="来信偏好"><summary>来信偏好<span>{paused ? "已暂停" : state.enabled ? state.presentation === "gentle" ? "首页轻提示" : "只放进信箱" : "未开启"}</span></summary>
            <label className="care-switch"><span>接受关怀来信</span><input type="checkbox" checked={state.enabled} disabled={busy} onChange={event => void update("preferences", event.target.checked)} /></label>
            <p>开启后，允许 AI 根据聊天和已授权的普通记录写来，相关内容发送到你配置的模型服务，可能产生额外调用费用。</p>
            {state.enabled && <>
              <label className="care-switch"><span>也参考我的私密日记</span><input type="checkbox" checked={state.include_private_diaries ?? false} disabled={busy} onChange={event => void update("preferences", true, undefined, event.target.checked)} /></label>
              <p>首页日记默认为私密。勾选后，允许将这些日记发送给当前模型用于关怀；后台从新写的日记开始检查。高度敏感内容始终不用于来信。</p>
              <label className="care-presentation">来信如何出现<AppSelect label="来信如何出现" value={state.presentation} disabled={busy} onChange={next => void update("preferences", true, next as "gentle" | "inbox")} options={[{value:"gentle",label:"首页轻提示"},{value:"inbox",label:"只放进信箱"}]} /></label>
              <small>最多三天一封 · 夜间不提示 · 不弹出系统通知<br />应用打开时检查新记录，信件由你点开阅读。</small>
              <button className="care-pause" disabled={busy} onClick={() => void update(paused ? "resume" : "pause")}>{paused ? "恢复来信" : "暂时不想收，暂停七天"}</button>
            </>}
          </details>
        </>}
      </div>
    </dialog>, document.body)}
  </>;
}
