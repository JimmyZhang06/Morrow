import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { CalendarDays, Check, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import "./Picker.css";

function Popup({ anchor, children, close, label }: {anchor: HTMLElement; children: ReactNode; close: () => void; label: string}) {
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({left: 0, top: 0, visibility: "hidden" as "hidden" | "visible"});
  useLayoutEffect(() => {
    const place = () => {
      const a = anchor.getBoundingClientRect(), p = panel.current!.getBoundingClientRect();
      setPosition({left: Math.max(8, Math.min(a.left, innerWidth - p.width - 8)), top: Math.max(8, a.bottom + p.height + 8 <= innerHeight ? a.bottom + 6 : a.top - p.height - 6), visibility: "visible"});
    };
    const observer = new ResizeObserver(place); observer.observe(panel.current!);
    place(); window.addEventListener("resize", place); window.addEventListener("scroll", place, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [anchor]);
  useEffect(() => {
    const outside = (event: PointerEvent) => { if (!panel.current?.contains(event.target as Node) && !anchor.contains(event.target as Node)) close(); };
    const focus = (event: FocusEvent) => { if (!panel.current?.contains(event.target as Node) && !anchor.contains(event.target as Node)) close(); };
    document.addEventListener("pointerdown", outside); document.addEventListener("focusin", focus);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("focusin", focus); };
  }, [anchor, close]);
  return createPortal(<div ref={panel} className="ink-picker-popup" style={position} role="dialog" aria-label={label} onKeyDown={e => {if(e.key === "Escape") {e.preventDefault(); e.stopPropagation(); close(); anchor.focus();}}}>{children}</div>, document.body);
}

export function AppSelect({ value, options, onChange, label, disabled = false }: { value: string; options: Array<{value: string; label: string}>; onChange: (value: string) => void; label: string; disabled?: boolean }) {
  const [open, setOpen] = useState(false), [active, setActive] = useState(0);
  const trigger = useRef<HTMLButtonElement>(null), list = useRef<HTMLDivElement>(null); const id=useId();
  const show = () => {setActive(Math.max(0,options.findIndex(o=>o.value===value)));setOpen(true);};
  useEffect(() => {if(open) list.current?.focus();}, [open]);
  useEffect(() => {list.current?.querySelector('[data-active="true"]')?.scrollIntoView({block:"nearest"});},[active]);
  const choose = (next: string) => { onChange(next); setOpen(false); trigger.current?.focus(); };
  return <><button type="button" className="ink-picker-trigger" ref={trigger} disabled={disabled} aria-label={label} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined} onClick={()=>open ? setOpen(false) : show()} onKeyDown={e=>{if(e.key==="ArrowDown" || e.key==="ArrowUp") {e.preventDefault();show();}}}><span>{options.find(o=>o.value===value)?.label || "请选择"}</span><ChevronDown size={16}/></button>{open && trigger.current && <Popup anchor={trigger.current} close={()=>setOpen(false)} label={label}><div id={id} ref={list} className="ink-picker-options" role="listbox" tabIndex={0} aria-label={label} aria-activedescendant={`${id}-${active}`} onKeyDown={e=>{
    if (["ArrowDown","ArrowUp","Home","End","Enter"," "].includes(e.key)) {e.preventDefault();if(e.key==="Enter"||e.key===" ") {if(options[active]) choose(options[active].value);} else setActive(i=> e.key==="Home" ? 0 : e.key==="End" ? options.length-1 : (i+(e.key==="ArrowDown"?1:-1)+options.length)%options.length);}
  }}>{options.map((o,i)=><div key={o.value} id={`${id}-${i}`} role="option" aria-selected={o.value===value} data-active={i===active} onMouseMove={()=>setActive(i)} onMouseDown={e=>e.preventDefault()} onClick={()=>choose(o.value)}>{o.label}{o.value===value && <Check size={15}/>}</div>)}</div></Popup>}</>;
}

const pad=(n:number)=>String(n).padStart(2,"0");
const stamp=(d:Date)=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const parse=(v:string)=>{const d=new Date(v); return Number.isNaN(d.getTime()) ? new Date() : d;};
export function DateTimePicker({value,onChange,label}:{value:string;onChange:(value:string)=>void;label:string}) {
  const trigger=useRef<HTMLButtonElement>(null), grid=useRef<HTMLDivElement>(null);
  const [open,setOpen]=useState(false), [draft,setDraft]=useState(()=>parse(value)), [month,setMonth]=useState(()=>parse(value));
  const [hour,setHour]=useState("00"), [minute,setMinute]=useState("00");
  const show=()=>{const d=parse(value);setDraft(d);setMonth(new Date(d.getFullYear(),d.getMonth(),1));setHour(pad(d.getHours()));setMinute(pad(d.getMinutes()));setOpen(true);};
  useEffect(()=>{if(open) grid.current?.querySelector<HTMLButtonElement>('[tabindex="0"]')?.focus();},[open]);
  const start=new Date(month.getFullYear(),month.getMonth(),1); start.setDate(1-(start.getDay()+6)%7);
  const days=Array.from({length:42},(_,i)=>new Date(start.getFullYear(),start.getMonth(),start.getDate()+i));
  const select=(d:Date)=>{setDraft(d);setMonth(new Date(d.getFullYear(),d.getMonth(),1));};
  const valid=/^\d{1,2}$/.test(hour)&&+hour<24&&/^\d{1,2}$/.test(minute)&&+minute<60;
  return <><button type="button" ref={trigger} className="ink-picker-trigger" aria-label={label} aria-haspopup="dialog" aria-expanded={open} onClick={()=>open?setOpen(false):show()}><span>{value ? value.replace("T"," ") : "选择日期和时间"}</span><CalendarDays size={17}/></button>{open&&trigger.current&&<Popup anchor={trigger.current} close={()=>setOpen(false)} label={`${label}日期与时间`}><div className="ink-calendar">
    <header><button type="button" aria-label="上个月" onClick={()=>setMonth(new Date(month.getFullYear(),month.getMonth()-1,1))}><ChevronLeft size={18}/></button><strong aria-live="polite">{month.getFullYear()} 年 {month.getMonth()+1} 月</strong><button type="button" aria-label="下个月" onClick={()=>setMonth(new Date(month.getFullYear(),month.getMonth()+1,1))}><ChevronRight size={18}/></button></header>
    <div className="ink-calendar-week" aria-hidden="true">{["一","二","三","四","五","六","日"].map(w=><span key={w}>{w}</span>)}</div>
    <div ref={grid} className="ink-calendar-days" role="group" aria-label="选择日期">{days.map(d=>{const selected=d.toDateString()===draft.toDateString();return <button type="button" key={d.toISOString()} tabIndex={selected || (!days.some(day=>day.toDateString()===draft.toDateString()) && d.getDate()===1) ? 0 : -1} aria-label={`${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`} aria-pressed={selected} data-outside={d.getMonth()!==month.getMonth()} onClick={()=>select(d)} onKeyDown={e=>{const delta=({ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7} as Record<string,number>)[e.key];if(delta){e.preventDefault();select(new Date(d.getFullYear(),d.getMonth(),d.getDate()+delta));requestAnimationFrame(()=>grid.current?.querySelector<HTMLButtonElement>('[tabindex="0"]')?.focus());}}}>{d.getDate()}</button>;})}</div>
    <div className="ink-calendar-time"><span>时间</span><input aria-label="小时" inputMode="numeric" maxLength={2} value={hour} onChange={e=>setHour(e.target.value)} /><span>:</span><input aria-label="分钟" inputMode="numeric" maxLength={2} value={minute} onChange={e=>setMinute(e.target.value)} /><small>24 小时制</small></div>
    {!valid&&<p role="status" className="ink-picker-error">小时为 0–23，分钟为 0–59</p>}
    <footer><button type="button" onClick={()=>select(new Date())}>今天</button><button type="button" className="ink-picker-confirm" disabled={!valid} onClick={()=>{const d=new Date(draft);d.setHours(+hour,+minute,0,0);onChange(stamp(d));setOpen(false);trigger.current?.focus();}}>确定</button></footer>
  </div></Popup>}</>;
}
