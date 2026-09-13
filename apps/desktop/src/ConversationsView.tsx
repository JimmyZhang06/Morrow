import { useEffect, useRef, useState } from "react";
import { cancelChat, createChat, deleteChat, getChat, listChats, renameChat, safeApiMessage, sendChat,
  localSearchStatus, searchDiaryHistory, type LocalSearchResult, type ChatDetail, type ChatSummary } from "./api";
import type { ApiSettings, Entry } from "./types";
import { PastLetter } from "./PastLetter";
import { ReplyText } from "./ReplyText";
import { StreamingAnswer } from "./StreamingAnswer";
import "./ConversationRefinement.css";

function failureMessage(code?: string | null) {
  if (!code || code === "provider.execution_unknown") return "未取得完整有效回答。";
  if (code.includes("json")) return "模型返回的内容格式不完整，未保存为有效回答。";
  if (code.includes("timeout")) return "等待模型响应超时。";
  if (code.includes("stream")) return "模型输出中断或流式格式不兼容。";
  if (code.includes("http_429")) return "模型服务暂时限流。";
  if (code.includes("http_401") || code.includes("http_403")) return "模型服务拒绝了当前凭据。";
  if (code.includes("authorization")) return "本次使用的材料或授权发生变化。";
  return "模型未返回可用结果。";
}

function preserveNewerTitle<T extends ChatSummary>(incoming: T, previous?: ChatSummary): T {
  return previous && previous.id === incoming.id && previous.title_revision > incoming.title_revision
    ? {...incoming, title: previous.title, title_revision: previous.title_revision, title_source: previous.title_source} : incoming;
}

export function ConversationsView({ settings, entries, available, onSettings, onSource, onRecords, onMemories }: {
  settings: ApiSettings; entries: Entry[]; available: boolean; onSettings: () => void; onSource: (id: string) => void; onRecords: () => void; onMemories: () => void;
}) {
  const [topic, setTopic] = useState("");
  const [suggestions, setSuggestions] = useState<LocalSearchResult | null>(null);
  const [useMemories, setUseMemories] = useState(true);
  const [autoRetrieve, setAutoRetrieve] = useState(true);
  const [experience, setExperience] = useState<"conversation" | "past_letter">("conversation");
  const recallVersion = useRef(0);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(() => sessionStorage.getItem("morrow.selectedChat"));
  useEffect(() => { if (selected) sessionStorage.setItem("morrow.selectedChat", selected); else sessionStorage.removeItem("morrow.selectedChat"); }, [selected]);
  const [detail, setDetail] = useState<ChatDetail | null>(null);
  const [materials, setMaterials] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [ready, setReady] = useState(false);
  const [listLoaded, setListLoaded] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const epoch = useRef(0);
  const requestId = useRef<string | null>(null);
  const turnsRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const stickToBottom = useRef(true);
  const [nameEditor, setNameEditor] = useState<{id: string; title: string; revision: number} | null>(null);
  const [nameBusy, setNameBusy] = useState(false);
  const [nameError, setNameError] = useState("");
  useEffect(() => {
    const input = composerRef.current;
    if (input) { input.style.height = "0px"; input.style.height = `${Math.min(96, Math.max(40, input.scrollHeight))}px`; }
  }, [draft, selected, !!detail]);
  useEffect(() => {
    if (stickToBottom.current && turnsRef.current) turnsRef.current.scrollTop = turnsRef.current.scrollHeight;
  }, [detail]);
  useEffect(() => { stickToBottom.current = true; composerRef.current?.focus(); }, [selected, !!detail]);
  useEffect(() => {
    if (!available) return;
    const current = ++epoch.current;
    let timer: ReturnType<typeof setTimeout>;
    setDetail(null); setError(""); setBusy(false); setConfirmDelete(false); setNameEditor(null); setNameError("");
    let nextListRefresh = 0;
    async function refresh() {
      const [list, chat] = await Promise.all([Date.now() >= nextListRefresh ? listChats(settings) : Promise.resolve(null), selected ? getChat(settings, selected) : Promise.resolve(null)]);
      if (epoch.current !== current) return;
      if (list?.ok) {
        setChats(previous => list.data.items.map(item => preserveNewerTitle(item, previous.find(old => old.id === item.id))));
        setReady(list.data.ready); setListLoaded(true); nextListRefresh = Date.now() + 5000;
      } else if (list) setError(safeApiMessage(list, "对话列表暂未加载"));
      if (chat?.status === 404) { setSelected(null); setDetail(null); }
      if (chat?.ok) {
        setDetail(previous => preserveNewerTitle(chat.data, previous ?? undefined));
        setChats(previous => previous.map(item => item.id === chat.data.id ? preserveNewerTitle({...item, title: chat.data.title, title_source: chat.data.title_source, title_revision: chat.data.title_revision}, item) : item));
      }
      else if (chat) {
        setError(safeApiMessage(chat, "对话暂未加载"));
        setDetail(previous => previous ? { ...previous, turns: previous.turns.map(turn => ({ ...turn, preview: "" })) } : previous);
      }
      timer = setTimeout(() => void refresh(), chat?.ok && chat.data.turns.some(turn => ["queued", "running"].includes(turn.state)) ? 350 : 1800);
    }
    void refresh();
    return () => { epoch.current++; clearTimeout(timer); };
  }, [settings, available, selected]);

  async function saveName() {
    if (!nameEditor || nameBusy || !nameEditor.title.trim()) return;
    const editor = nameEditor, current = epoch.current;
    setNameBusy(true); setNameError("");
    try {
      const response = await renameChat(settings, editor.id, editor.title.trim(), editor.revision);
      if (epoch.current !== current) return;
      if (response.ok) {
        setChats(previous => previous.map(item => item.id === editor.id ? response.data : item));
        setDetail(previous => previous?.id === editor.id ? {...previous, ...response.data} : previous);
        setNameEditor(null);
      } else {
        setNameError(safeApiMessage(response, "名称未保存，请重试"));
        if (response.status === 409) {
          const latest = await getChat(settings, editor.id);
          if (epoch.current === current && latest.ok) {
            setDetail(latest.data);
            setNameEditor(previous => previous?.id === editor.id ? {...previous, revision: latest.data.title_revision} : previous);
          }
        }
      }
    } finally { setNameBusy(false); }
  }

  async function findMaterials() {
    if (busy || !topic.trim()) return;
    const current = epoch.current, version = ++recallVersion.current;
    setBusy(true); setError("");
    try {
      const permission = await localSearchStatus(settings);
      if (current !== epoch.current || version !== recallVersion.current) return;
      if (!permission.ok || !permission.data.enabled) {
        setError("请先到记录页开启本地搜索并补建索引，再查找相关日记。"); return;
      }
      const response = await searchDiaryHistory(settings, topic);
      if (current !== epoch.current || version !== recallVersion.current) return;
      if (response.ok) {
        setSuggestions(response.data);
        setMaterials([...new Set(response.data.items.map(hit => hit.entry_id))].slice(0, 8));
        setConsent(false);
      } else setError(safeApiMessage(response, "相关日记暂未找到"));
    } finally { if (current === epoch.current) setBusy(false); }
  }
  async function act(kind: "create" | "send" | "cancel" | "delete") {
    if (busy) return;
    const current = epoch.current;
    setBusy(true); setError("");
    try {
      if (kind === "create") {
        const result = await createChat(settings, materials, useMemories);
        if (current !== epoch.current) return;
        if (result.ok) { setSelected(result.data.id); setDraft(topic); requestId.current = null; }
        else setError(safeApiMessage(result, "对话未创建"));
      } else if (selected) {
        if (kind === "send") requestId.current ??= crypto.randomUUID();
        const result = kind === "send" ? await sendChat(settings, selected, draft, requestId.current!, autoRetrieve, experience)
          : kind === "cancel" ? await cancelChat(settings, selected) : await deleteChat(settings, selected);
        if (current !== epoch.current) return;
        if (!result.ok) setError(safeApiMessage(result, "操作未完成，输入已保留"));
        else if (kind === "delete") { setSelected(null); setDetail(null); }
        else {
          if (kind === "send") { setDraft(""); setExperience("conversation"); requestId.current = null; }
          const response = await getChat(settings, selected);
          if (current === epoch.current && response.ok) setDetail(response.data);
        }
      }
    } finally { if (current === epoch.current) setBusy(false); }
  }
  const active = detail?.turns.some(turn => ["queued", "running"].includes(turn.state));
  const eligible = entries.filter(entry => entry.syncState === "synced" && entry.source_type === "note" && entry.data_class !== "highly_sensitive");
  return <div className="content-page chat-page page-enter">
    <header className="chat-page-heading"><h1>对话</h1><span>从经历里，慢慢看清自己</span></header>
    {!available ? <p>当前后端暂不支持对话，请更新本地服务。</p> : <>
      {listLoaded && !ready && <div className="local-prototype-note"><p>请先配置并开启在线模型，已有对话仍保存在本机。</p><button onClick={onSettings}>前往模型设置</button></div>}
      <div className={`chat-layout${!chats.length && !selected ? " no-history" : ""}`}>
        <aside aria-label="已保存的对话"><button className="primary-action" onClick={() => { setSelected(null); setDetail(null); setExperience("conversation"); setConsent(false); setMaterials([]); setDraft(""); setTopic(""); setSuggestions(null); recallVersion.current++; requestId.current = null; }}>新建对话</button>
          <span className="chat-history-label">最近对话</span>
          {!chats.length && <p className="chat-history-empty">聊过的内容会留在这里，随时可以接着聊。</p>}
          {chats.map(chat => <button className={chat.id === selected ? "chat-session is-active" : "chat-session"} key={chat.id} aria-current={chat.id === selected ? "page" : undefined}
            onClick={() => { if (chat.id !== selected) { setSelected(chat.id); setDraft(""); setExperience("conversation"); setAutoRetrieve(false); requestId.current = null; } }}><span>{chat.title || "一段新的对话"}</span><small>{new Date(chat.created_at).toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small></button>)}
        </aside>
        <section className="chat-main" aria-label="AI 对话">
          {!selected ? <div className="chat-setup">
            <div className="chat-welcome"><span className="chat-avatar" aria-hidden="true">m</span><span className="eyebrow">MORROW · 陪你回看，也陪你向前</span><h2>这次想聊什么？</h2><p>一个困惑，一点变化，或者最近放不下的事。</p></div>
            <button className="past-letter-entry" aria-pressed={experience === "past_letter"} onClick={() => { setExperience(experience === "past_letter" ? "conversation" : "past_letter"); setAutoRetrieve(true); setConsent(false); }}><span aria-hidden="true">✉</span><span><strong>向过去借一点力量</strong><small>用你曾经写下的日记，回应今天的困惑</small></span><span>{experience === "past_letter" ? "已选择" : "试试看 →"}</span></button>
            {experience === "past_letter" && <label className="past-letter-topic">今天有什么事，让你想听听过去的自己？<textarea value={topic} maxLength={500} onChange={event => setTopic(event.target.value)} placeholder="比如：又在担心项目做不好，想起以前也有过这种时候…" /></label>}
            {experience === "conversation" && <label className="chat-start-topic">现在想聊的事 <span>可先留空</span><textarea aria-label="这次想聊什么" value={topic} maxLength={500} onChange={event => setTopic(event.target.value)} placeholder="最近发生了什么？或者，有什么想一起梳理的？" /></label>}
            <details className="chat-context-options"><summary>这次参考什么<span>{autoRetrieve ? "相关日记" : "不自动查找"} · {useMemories ? "已确认的认识" : "不带入认识"}{materials.length ? ` · 手选 ${materials.length} 篇` : ""}</span></summary>
            <label className="chat-auto-option"><input type="checkbox" checked={autoRetrieve} disabled={experience === "past_letter"} onChange={event => { setAutoRetrieve(event.target.checked); setConsent(false); }} /><span><strong>自动查找相关日记</strong><small>每轮补充已授权的相关日记。可在记录页开启本地搜索。</small></span></label>
            <details className="chat-manual-recall"><summary>选择参考日记 <span>已选 {materials.length} / 8 · 可选</span></summary><div className="chat-recall">
              <label htmlFor="chat-topic">先写下问题，帮你找相关经历</label>
              <input id="chat-topic" value={topic} maxLength={500} placeholder="例如：最近什么时候更容易开始做事？"
                onChange={event => { setTopic(event.target.value); recallVersion.current++; setSuggestions(null); setMaterials([]); setConsent(false); }} />
              <button disabled={busy || !topic.trim()} onClick={() => void findMaterials()}>查找相关日记</button>
              <button onClick={onRecords}>管理本地搜索</button>
              <p>只在本机按关键词找材料，不调用模型。查看并调整选择后再开始对话。</p>
              {suggestions && <>
                <p role="status">本次检查 {suggestions.scanned} 个片段，可用索引 {suggestions.indexed} 个，找到 {suggestions.items.length} 个片段。{suggestions.pending > 0 ? `另有 ${suggestions.pending} 个待补建。` : ""}{suggestions.truncated ? "仅覆盖最近 1000 个片段。" : ""}{suggestions.items.length === 0 ? "未找到不代表没有相关经历，可换关键词或手动选日记。" : ""}</p>
                {suggestions.items.map(hit => <label className="chat-recall-hit" key={hit.fragment_id}>
                  <input type="checkbox" checked={materials.includes(hit.entry_id)} disabled={!materials.includes(hit.entry_id) && materials.length >= 8}
                    onChange={event => { setConsent(false); setMaterials(current => event.target.checked ? [...new Set([...current, hit.entry_id])] : current.filter(id => id !== hit.entry_id)); }} />
                  <span>{hit.excerpt}</span>
                </label>)}
              </>}
            </div>
            <section className="chat-material-picker">
            <h2>确认这次带上的经历（已选 {materials.length} 条）</h2><p>手选日记会固定带入每轮。开启自动查找后，每轮补充相关日记，合计最多 8 条。</p>
            <div className="chat-materials">{eligible.filter(entry => !suggestions?.items.some(hit => hit.entry_id === entry.id)).map(entry => <label key={entry.id}>
              <input type="checkbox" checked={materials.includes(entry.id)} disabled={!materials.includes(entry.id) && materials.length >= 8}
                onChange={event => { setConsent(false); setMaterials(current => event.target.checked ? [...current, entry.id] : current.filter(id => id !== entry.id)); }} />
              <span>{new Date(entry.captured_at).toLocaleDateString()} · {entry.content.slice(0, 110)}</span>
            </label>)}{!eligible.length && <p>还没有可选日记。可以直接聊，也可以先去记录一些经历。<button onClick={onRecords}>去记录</button></p>}</div></section></details>
            <label className="chat-memory-option"><input type="checkbox" checked={useMemories} onChange={event => { setUseMemories(event.target.checked); setConsent(false); }} />
              <span>参考已确认或纠正的认识；不使用未确认、已拒绝或撤回的内容。</span></label>
            </details>
            <label className="chat-consent"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} />
              <span>允许所选日记{autoRetrieve ? "、每轮自动检索到的相关日记" : ""}、本会话及勾选的关联认识和纠正原话发送给当前在线模型。AI 回答不会自动成为关于我的认识。</span></label>
            <button className="primary-action" disabled={!ready || !consent || busy} onClick={() => void act("create")}>开始对话</button>
          </div> : <>
            <div className="chat-toolbar"><div>{nameEditor ? <form className="chat-name-editor" onSubmit={event => { event.preventDefault(); void saveName(); }}>
              <input autoFocus aria-label="对话名称" value={nameEditor.title} maxLength={60} disabled={nameBusy} onChange={event => setNameEditor({...nameEditor, title: event.target.value})} onKeyDown={event => { if (event.key === "Escape") { setNameEditor(null); setNameError(""); } }} />
              <button disabled={nameBusy || !nameEditor.title.trim()} type="submit">保存名称</button><button type="button" disabled={nameBusy} onClick={() => {setNameEditor(null); setNameError("");}}>取消</button>
              {nameError && <p role="alert">{nameError}</p>}
            </form> : <div className="chat-title-line"><strong title={detail?.title}>{detail?.title || chats.find(chat => chat.id === selected)?.title || "正在打开对话…"}</strong><button aria-label="重命名对话" title="重命名对话" disabled={!detail} onClick={() => { if (detail) { setNameEditor({id: detail.id, title: detail.title, revision: detail.title_revision}); setNameError(""); } }}>✎</button></div>}<span>手动选用 {detail?.entry_ids.length ?? "…"} 条日记{autoRetrieve ? " · 自动查找已开启" : ""}</span></div>
              <div className="chat-toolbar-actions">{detail?.include_reviewed_memories && <button onClick={onMemories}>查看和纠正认识</button>}<button disabled={busy} onClick={() => confirmDelete ? void act("delete") : setConfirmDelete(true)}>{confirmDelete ? "确认删除此对话" : "删除对话"}</button>{confirmDelete && <button onClick={() => setConfirmDelete(false)}>取消</button>}</div></div>
            {detail?.blocked && <p role="status">材料或授权已变化，相关回答已隐藏。请确认授权，或重新选择日记建立对话。</p>}
            <div className="chat-turns" ref={turnsRef} onScroll={event => { const el = event.currentTarget; stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
              {detail && !detail.turns.length && <div className="chat-empty-conversation"><span className="chat-avatar" aria-hidden="true">m</span><h2>从你此刻的想法开始</h2><p>{detail.entry_ids.length ? "日记已经准备好。你想从哪件事聊起？" : "不必整理好思路，先说说最近的感受。"}</p><div>{["最近有什么变化值得我留意？", "帮我梳理一下现在的困惑"].map(prompt => <button key={prompt} onClick={() => { setDraft(prompt); requestId.current = null; composerRef.current?.focus(); }}>{prompt}</button>)}</div></div>}
              {!detail && <p role="status">正在打开对话…</p>}
              {detail?.turns.map(turn => <article key={turn.id} id={`chat-turn-${turn.id}`}>
              <div className="chat-question"><strong>你</strong><p>{turn.question}</p></div>
              <div className="chat-answer"><strong>Morrow</strong>{turn.reply && turn.retrieval?.experience === "past_letter" ? <PastLetter reply={turn.reply} onSource={onSource} /> : turn.reply ? <>
                <ReplyText text={turn.reply.answer} /><details className="chat-uncertainty"><summary>依据与说明</summary><p>{turn.reply.uncertainty}</p>
                {turn.retrieval && <p className="chat-retrieval-status">本轮自动找到 {turn.retrieval.matched} 条日记{turn.retrieval.indexed === 0 ? " · 暂无可搜索的索引，可到记录页开启搜索并补建" : ""}{turn.retrieval.pending > 0 ? ` · ${turn.retrieval.pending} 条待索引` : ""}{turn.retrieval.truncated ? " · 检索范围有限" : ""}</p>}</details>
                {(turn.reply.reviewed_memories?.length ?? 0) > 0 && <details><summary>本轮参考的已裁定认识</summary>{turn.reply.reviewed_memories!.map(memory => <p key={memory.memory_id}>{memory.review === "correct" ? "已纠正" : "已确认"} · 第 {memory.version} 版：{memory.statement}</p>)}</details>}
                {!!turn.reply.citations.length && <details className="chat-sources"><summary>参考来源 · {new Set(turn.reply.citations.map(citation => citation.entry_id)).size} 篇</summary>{[...new Set(turn.reply.citations.map(citation => citation.entry_id))].map((entryId, i) => <blockquote key={entryId}>{[...new Set(turn.reply!.citations.filter(citation => citation.entry_id === entryId).map(citation => citation.quote))].map(quote => <p key={quote}>{quote}</p>)}<button onClick={() => { const citation = turn.reply!.citations.find(item => item.entry_id === entryId); if (citation?.turn_id) document.getElementById(`chat-turn-${citation.turn_id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); else onSource(entryId); }}>{turn.reply!.citations.some(item => item.entry_id === entryId && item.turn_id) ? "回到这条消息" : "查看日记原文"} {i + 1}</button></blockquote>)}</details>}
              </> : turn.partial ? <><ReplyText text={turn.partial.answer} /><div className="chat-interrupted" role="status"><span>回答暂时停在这里，已收到的内容已保留。你可以接着聊。</span><button disabled={busy || active} onClick={() => { setDraft("请接着刚才未完成的回答继续，不必重复已经说过的内容。"); requestId.current = null; composerRef.current?.focus(); }}>接着说</button></div></> : turn.state === "running" && turn.preview ? <StreamingAnswer text={turn.preview} onProgress={() => { if (stickToBottom.current && turnsRef.current) turnsRef.current.scrollTop = turnsRef.current.scrollHeight; }} /> : <p role="status">{turn.state === "queued" ? "正在准备相关日记…" : turn.state === "running" ? "正在结合所选材料思考…" : turn.state === "outdated" ? "相关认识已更新，旧回答已隐藏且不会作为后续依据。可以继续提问。" : turn.state === "unknown" ? (failureMessage(turn.failure_code) + " 原问题已保存，可以再试一次。") : turn.state === "failed" ? (failureMessage(turn.failure_code) + " 输入已保存，可以重新编辑后发送。") : turn.state === "canceled" ? "本次生成已取消或材料已变化。" : "回答因材料或授权变化而暂不可用。"}</p>}{["failed", "unknown"].includes(turn.state) && !turn.partial && <button className="chat-retry-draft" disabled={busy || active} onClick={() => { setDraft(turn.question); requestId.current = null; composerRef.current?.focus(); }}>重新填入输入框</button>}</div>
            </article>)}</div>
            {detail && detail.turns.length >= detail.max_turns ? <div className="chat-limit">本会话已达到 {detail.max_turns} 轮上限，可以新建对话继续。{active && <button disabled={busy} onClick={() => void act("cancel")}>停止生成</button>}</div> : <form className="chat-composer" onSubmit={event => { event.preventDefault(); if (ready && !busy && !active && detail && !detail.blocked && draft.trim()) { stickToBottom.current = true; void act("send"); } }}>
              <textarea ref={composerRef} aria-label="对话问题" placeholder="说说你的想法…" maxLength={3000} value={draft}
                onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
                onChange={event => { setDraft(event.target.value); requestId.current = null; }} />
              <div className="chat-composer-footer"><details className="chat-composer-options"><summary>选项 · {experience === "past_letter" ? "回信模式" : autoRetrieve ? "自动查找" : "手动选材"}</summary><div className="chat-composer-popover">
              <div className="chat-experience-switch"><button type="button" aria-pressed={experience === "past_letter"} disabled={busy || active} onClick={() => { setExperience(experience === "past_letter" ? "conversation" : "past_letter"); setAutoRetrieve(true); requestId.current = null; }}>{experience === "past_letter" ? "✉ 回信模式 · 点击退出" : "✉ 向过去借一点力量"}</button></div>
              <label className="chat-auto-inline"><input type="checkbox" checked={autoRetrieve} disabled={busy || active || experience === "past_letter"} onChange={event => { setAutoRetrieve(event.target.checked); requestId.current = null; }} />允许自动查找相关日记并发送给模型</label>
              </div></details><span>Enter 发送 · Shift + Enter 换行</span>{active ? <button type="button" disabled={busy} onClick={() => void act("cancel")}>停止生成</button> : <button className="primary-action" disabled={!ready || busy || !detail || detail.blocked || !draft.trim()}>发送</button>}</div>
            </form>}
          </>}
          {error && <p role="alert">{error}</p>}
        </section>
      </div>
    </>}
  </div>;
}
