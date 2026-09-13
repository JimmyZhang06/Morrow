import type { ReactNode } from "react";

// React escapes all text. Only emphasis and inline code are recognized; no HTML.
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part, index) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part.startsWith("`") && part.endsWith("`") ? <code key={index}>{part.slice(1, -1)}</code>
        : part);
}

export function ReplyText({ text }: { text: string }) {
  return <div className="reply-text">{text.split(/\n\s*\n/).filter(Boolean).map((part, i) => <p key={i}>{inline(part)}</p>)}</div>;
}
