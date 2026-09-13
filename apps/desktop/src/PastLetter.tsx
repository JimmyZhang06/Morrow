import { useEffect, useId, useRef, useState } from "react";
import { flushSync } from "react-dom";
import type { ChatDetail } from "./api";
import "./PastLetter.css";
import { ReplyText } from "./ReplyText";

function Envelope() {
  return <div className="envelope-stage" aria-hidden="true"><div className="envelope-shadow" /><div className="envelope-object">
    <div className="envelope-back" />
    <div className="envelope-sheet"><span>MORROW</span><i>写给此刻的你</i><div /><div /><div /><small>有些力量，你曾经写下过。</small></div>
    <div className="envelope-front"><div className="envelope-address"><small>A LETTER FROM YOUR DAYS</small><strong>致，此刻的你</strong><span>寄自那些被你认真记下的日子</span></div></div>
    <div className="envelope-flap"><span className="envelope-flap-outside" /><span className="envelope-flap-inside" /></div>
    <div className="envelope-wax">m</div>
  </div></div>;
}

export function PastLetter({ reply, onSource }: { reply: NonNullable<ChatDetail["turns"][number]["reply"]>; onSource: (id: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const reading = useRef<HTMLHeadingElement>(null);
  const transition = useRef<ViewTransition | null>(null);
  const [phase, setPhase] = useState<"closed" | "sealed" | "opening" | "reading">("closed");
  useEffect(() => {
    if (phase !== "opening") return;
    let current = true;
    const preference = matchMedia("(prefers-reduced-motion: reduce)");
    const finish = () => {
      if (!current) return;
      if (preference.matches || !document.startViewTransition) { setPhase("reading"); return; }
      transition.current = document.startViewTransition(() => {
        if (current) flushSync(() => setPhase("reading"));
      });
      void transition.current.ready.catch(() => {});
    };
    const timer = setTimeout(finish, preference.matches ? 0 : 3400);
    const changed = () => { if (preference.matches) finish(); };
    preference.addEventListener("change", changed);
    return () => { current = false; clearTimeout(timer); preference.removeEventListener("change", changed); };
  }, [phase]);
  useEffect(() => { if (phase === "reading") reading.current?.focus(); }, [phase]);
  const close = () => { transition.current?.skipTransition(); dialog.current?.close(); setPhase("closed"); trigger.current?.focus(); };
  const quotes = reply.citations.filter((citation, i, all) => all.findIndex(item => item.entry_id === citation.entry_id && item.quote === citation.quote) === i);
  const sources = [...new Set(quotes.map(quote => quote.entry_id))].map(entryId => ({
    entryId, quotes: quotes.filter(quote => quote.entry_id === entryId),
  }));
  if (!quotes.length) return <div className="past-letter-empty"><strong>这次还没有找到合适的旧日记</strong><p>可以换一个具体关键词，或手动带上一篇日记，再向过去借一点力量。</p></div>;
  return <div className="past-letter">
    <button className="letter-preview" ref={trigger} onClick={() => { setPhase("sealed"); dialog.current?.showModal(); }} aria-label="查看回信信封"><Envelope /><div className="letter-preview-caption"><span>一封只与你有关的信</span><strong>向过去，借一点力量</strong><small>点击收信 · {new Set(quotes.map(quote => quote.entry_id)).size} 篇日记留下的线索</small></div></button>
    <dialog className={`letter-dialog phase-${phase}`} ref={dialog} aria-labelledby={titleId} onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
      <div className="letter-dialog-shell"><div className="letter-dialog-bar"><span>MORROW / 时光邮局</span><button onClick={close} aria-label="关闭回信">×</button></div>
        {phase !== "reading" ? <div className="letter-unwrapping">
          <header className="letter-scene-heading"><span className="letter-overline">一封只与你有关的信</span><h2 id={titleId}>向过去，借一点力量</h2></header>
          <Envelope />
          <div className="letter-scene-actions"><p className="letter-stage-caption" role="status">{phase === "opening" ? "正在取出你留下的话…" : "那些认真写下的日子，正在给你回信。"}</p><button className="primary-action" onClick={() => setPhase(phase === "opening" ? "reading" : "opening")}>{phase === "opening" ? "跳过动画，直接阅读" : "拆开回信"}</button><small className="letter-authorship">日记是你的原话 · 回信由 AI 连接与书写</small></div>
        </div> : <>
          <div className="letter-reading-scroll"><article className="past-letter-paper">
            <header className="letter-paper-heading"><div className="letter-paper-meta"><span>MORROW · 私人回信</span><span>依据 {new Set(quotes.map(quote => quote.entry_id)).size} 篇日记</span></div><h2 id={titleId} ref={reading} tabIndex={-1}>亲爱的，此刻的你：</h2><p>以下是 AI 读过你的日记后，写给今天的你的话。</p></header>
            <section className="letter-thoughts" aria-label="AI 生成的回信"><div className="past-letter-body"><ReplyText text={reply.answer} /></div></section>
            <div className="letter-signoff"><span className="letter-signature">Morrow</span><p>过去留下线索，今天由你来回答。</p></div>
            <section className="letter-originals" aria-label="日记原话">
              <details className="letter-originals-details" open>
                <summary><strong>这封信的原文依据</strong><span>{sources.length} 篇日记 · {quotes.length} 段摘录</span><i aria-hidden="true" /></summary>
                <p className="letter-source-hint">以下都是你的原话，按日记整理。</p>
                <div className="letter-source-list">{sources.map((source, i) => <div className="letter-source-note" key={source.entryId}>
                  <header><span className="letter-source-index">{String(i + 1).padStart(2, "0")}</span><span>日记摘录</span><button aria-label={`查看日记 ${i + 1} 原文`} onClick={() => { close(); onSource(source.entryId); }}>查看原文 <span aria-hidden="true">↗</span></button></header>
                  <div className="letter-source-quotes">{source.quotes.map(citation => <blockquote key={`${citation.fragment_id}:${citation.quote}`}><p>{citation.quote}</p></blockquote>)}</div>
                </div>)}</div>
              </details>
            </section>
            <aside className="letter-margin-note"><strong>留一点余地</strong><p>{reply.uncertainty}</p></aside>
          </article></div>
          <footer className="letter-reader-actions"><span>读完之后，也可以继续聊聊。</span><button className="primary-action" onClick={close}>收好回信，继续聊</button></footer>
        </>}

      </div>
    </dialog>
  </div>;
}
