import { useEffect, useState } from "react";
import "./StreamingAnswer.css";

export function StreamingAnswer({ text, onProgress }: { text: string; onProgress: () => void }) {
  const [shown, setShown] = useState("");
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(text);
      return;
    }
    const timer = window.setInterval(() => {
      setShown(previous => {
        if (!text.startsWith(previous)) return "";
        const tail = Array.from(text.slice(previous.length));
        return previous + tail.slice(0, Math.max(1, Math.ceil(tail.length / 12))).join("");
      });
    }, 24);
    return () => window.clearInterval(timer);
  }, [text]);
  useEffect(() => { onProgress(); }, [shown, onProgress]);
  return <div className="chat-stream-preview"><p>{text.startsWith(shown) ? shown : ""}<span className="chat-stream-caret" aria-hidden="true" /></p><span role="status">正在生成 · 正文为草稿，引用校验后显示</span></div>;
}
