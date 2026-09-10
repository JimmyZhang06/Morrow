import { useEffect, useState } from "react";

const BACKUP_KEYS = ["vistora.localEntries", "vistora.recordDraft", "vistora.localActions.v2"];

export function LocalSettings({ onRefresh }: { onRefresh: () => void }) {
  const [status, setStatus] = useState<DesktopModelStatus | null>(null);
  const [profileId, setProfileId] = useState("new");
  const [name, setName] = useState("");
  const [protocol, setProtocol] = useState("compatible");
  const [baseUrl, setBaseUrl] = useState("");
  const [jsonMode, setJsonMode] = useState(false);
  const [key, setKey] = useState("");
  const [model, setModel] = useState("step-3.7-flash");
  const [consent, setConsent] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [deletePending, setDeletePending] = useState(false);
  const selectProfile = (value?: DesktopModelProfile) => {
    setProfileId(value?.id || "new"); setName(value?.name || "");
    setProtocol(value?.protocol || "compatible"); setBaseUrl(value?.baseUrl || "");
    setModel(value?.model || ""); setJsonMode(value?.jsonMode || false);
    setKey(""); setConsent(false); setMessage("");
    setDeletePending(false);
  };
  useEffect(() => {
    void window.vistoraDesktop?.localStatus().then((value) => {
      setStatus(value);
      if (value) selectProfile(value.profiles.find(p => p.id === value.activeProfileId));
    }).catch(() => setMessage("本地设置暂不可用，请重启后重试"));
    const timer = setInterval(() => {
      void window.vistoraDesktop?.localStatus().then(setStatus).catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, []);
  if (!status) return null;
  const selected = status.profiles.find(p => p.id === profileId);
  const canReuseKey = selected?.hasKey && selected.baseUrl === baseUrl.trim().replace(/\/+$/, "") && selected.protocol === protocol;

  const deleteProfile = async () => {
    if (!selected) return;
    setBusy(true); setMessage("");
    try {
      const result = await window.vistoraDesktop!.localMaintenance({ operation: "ai", enabled: false, deleteProfileId: selected.id });
      if (!result.ok) { setMessage(result.error || "配置未删除"); return; }
      setStatus(result.status!);
      selectProfile(result.status!.profiles.find(p => p.id === result.status!.activeProfileId));
      setMessage("配置及其密钥已删除。"); onRefresh();
    } catch { setMessage("删除未能确认，请刷新后检查配置"); }
    finally { setBusy(false); }
  };

  const configure = async (enabled: boolean) => {
    setBusy(true); setMessage("");
    try {
      const result = await window.vistoraDesktop!.localMaintenance({ operation: "ai", enabled, consent, key, model, profileId, name, protocol, baseUrl, jsonMode });
      if (!result.ok) setMessage(result.error || "AI 设置未保存");
      else {
        setStatus(result.status!); setKey(""); setConsent(false);
        if (enabled) setProfileId(result.status!.activeProfileId);
        setMessage(enabled ? "模型配置已启用。请从一条记录开始整理，调用费用由你的模型账户承担；切换前未完成的任务不会转发到新服务。" : "在线 AI 已关闭，当前配置的 API Key 已移除。其他已保存配置保留。");
        onRefresh();
      }
    } catch { setMessage("设置未能确认，请刷新后检查当前状态"); }
    finally { setBusy(false); }
  };

  const backup = async (operation: "backup" | "restore") => {
    setBusy(true); setMessage("");
    try {
      const clientState = Object.fromEntries(BACKUP_KEYS.flatMap((name) => {
        const value = localStorage.getItem(name);
        return value === null ? [] : [[name, value]];
      }));
      const result = await window.vistoraDesktop!.localMaintenance({ operation, password, clientState });
      if (result.canceled) return;
      if (!result.ok) { setMessage(result.error || "备份操作未完成"); return; }
      setPassword("");
      if (operation === "restore") {
        for (const name of BACKUP_KEYS) {
          const value = result.clientState?.[name];
          if (value === undefined) localStorage.removeItem(name); else localStorage.setItem(name, value);
        }
        localStorage.removeItem("vistora.candidateJobs.v1");
        localStorage.removeItem("vistora.candidateRequestKeys");
        window.location.reload();
      } else setMessage("加密备份已保存。请将文件和密码分开保管；删除记录不会删除你导出的备份。");
    } catch { setMessage("操作未能确认，请检查备份文件后重试"); }
    finally { setBusy(false); onRefresh(); }
  };

  return <>
    <section className="settings-section local-runtime-settings">
      <div className="settings-section-heading"><div><span><strong>在线 AI</strong><small>{status.enabled ? "已开启 · 使用你的模型账户" : "默认关闭 · 不配置也能记录"}</small></span></div></div>
      <p>记录保存在这台电脑。开启后，你主动整理的记录及相关支持材料会发送到所选 API 服务，用于生成候选认识、行动或叙事。输出需要你核对，不构成诊断。</p>
      <p>支持 OpenAI 兼容的 Chat Completions API 和 Step Plan。供应商的处理地区、保留期限和训练使用规则由其决定，本应用不保证零保留或禁止训练。原生 Anthropic / Gemini 等其他协议需使用兼容接口。</p>
      {status.enabled && <p>当前使用：{status.profiles.find(p => p.id === status.activeProfileId)?.name} · {status.model}</p>}
      {status.issue && <p role="alert">{status.issue}</p>}
      <div className="model-library">
        <div className="model-library-heading"><div><strong>我的模型</strong><span>{status.profiles.length ? `${status.profiles.length} 个已保存配置` : "添加你的第一个模型服务"}</span></div><button type="button" className="model-add" disabled={busy} onClick={() => selectProfile()}>＋ 新增配置</button></div>
        {status.profiles.length ? <div className="model-profile-list" role="group" aria-label="已保存的模型配置">{status.profiles.map(p => {
          let host = p.baseUrl; try { host = new URL(p.baseUrl).host; } catch { /* Legacy display only. */ }
          const active = status.enabled && p.id === status.activeProfileId;
          return <button type="button" className={`model-profile-card${p.id === profileId ? " is-selected" : ""}`} key={p.id} aria-label={`编辑配置：${p.name}`} aria-pressed={p.id === profileId} disabled={busy} onClick={() => selectProfile(p)}><span className="model-profile-top"><strong>{p.name}</strong>{active ? <span className="model-profile-badge">使用中</span> : <span className="model-profile-state">{p.hasKey ? "已保存" : "待填写密钥"}</span>}</span><span className="model-profile-model" title={p.model}>{p.model}</span><span className="model-profile-host" title={host}>{host}</span></button>;
        })}</div> : <div className="model-profile-empty">还没有模型配置。填写下方信息，连接你自己的 AI 服务。</div>}
        <p className="model-library-hint">选择配置可查看或编辑；保存后才会切换当前模型。</p>
      </div>
      <div className="model-editor-heading"><strong>{selected ? `编辑配置 · ${selected.name}` : "新增模型配置"}</strong>{selected && <button type="button" disabled={busy} onClick={() => setDeletePending(!deletePending)}>删除配置</button>}</div>
      {deletePending && selected && <div className="model-delete-confirm" role="group" aria-label="删除配置确认"><p>删除「{selected.name}」及其密钥？{status.enabled && selected.id === status.activeProfileId ? "当前 AI 将关闭。" : ""}已有记录和认识会保留。</p><div><button type="button" disabled={busy} onClick={() => setDeletePending(false)}>取消</button><button type="button" disabled={busy} onClick={() => void deleteProfile()}>确认删除</button></div></div>}
      <label className="settings-field"><span>配置名称</span><input value={name} maxLength={80} onChange={event => setName(event.target.value)} disabled={busy} placeholder="例如：日常整理 / 深度思考" /></label>
      <label className="settings-field"><span>API 协议</span><select value={protocol} disabled={busy} onChange={event => { setProtocol(event.target.value); setKey(""); setConsent(false); }}><option value="compatible">OpenAI 兼容 · Chat Completions</option><option value="stepfun">Step Plan 专用</option></select></label>
      <label className="settings-field"><span>API 服务地址（Base URL）</span><input value={baseUrl} type="url" autoComplete="off" spellCheck={false} placeholder="https://你的服务地址/v1" onChange={event => { setBaseUrl(event.target.value); setConsent(false); }} disabled={busy} /></label>
      <small>填写供应商给出的 API 根地址，应用会追加 /chat/completions。修改地址后请重新填写密钥。</small>
      <label className="settings-field"><span>API Key {canReuseKey ? "（已安全保存，留空保留）" : ""}</span><input type="password" autoComplete="off" spellCheck={false} value={key} onChange={(event) => setKey(event.target.value)} disabled={busy} /></label>
      <label className="settings-field"><span>模型名称</span><input value={model} onChange={(event) => setModel(event.target.value)} disabled={busy} /></label>
      {protocol === "compatible" && <label className="local-consent"><input type="checkbox" checked={jsonMode} onChange={event => setJsonMode(event.target.checked)} disabled={busy} />启用 JSON 模式（仅在供应商支持 response_format 时勾选）</label>}
      <label className="local-consent"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} disabled={busy} />我同意相关材料发送到以上 API 地址，并接受该供应商的数据保留、可能的训练使用及计费规则。</label>
      <div className="settings-actions"><button className="primary-action" disabled={busy || !consent || !name.trim() || !baseUrl.trim() || !model.trim() || (!key.trim() && !canReuseKey)} onClick={() => void configure(true)}>{busy ? "正在处理…" : "保存并切换到此模型"}</button>{status.enabled && <button disabled={busy} onClick={() => void configure(false)}>关闭 AI 并移除当前密钥</button>}</div>
      <small>密钥由 Windows 当前账户加密保护，不随备份导出。关闭 AI 不会删除已有认识。</small>
    </section>
    <section className="settings-section local-runtime-settings">
      <div className="settings-section-heading"><div><span><strong>备份与恢复</strong><small>加密保存记录、认识、行动和本机草稿</small></span></div></div>
      <p>请定期备份到另一个位置。恢复会切换到备份数据并关闭在线 AI；原数据库保留用于回退。旧版原始文件、历史备份和回退数据库不会随单条记录删除而清除。</p>
      <label className="settings-field"><span>备份密码（至少 12 个字符，无法找回）</span><input type="password" autoComplete="off" value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} /></label>
      <div className="settings-actions"><button disabled={busy || password.length < 12} onClick={() => void backup("backup")}>导出加密备份</button><button disabled={busy || password.length < 12} onClick={() => void backup("restore")}>从备份恢复</button></div>
    </section>
    {message && <p role="status" className="local-settings-message">{message}</p>}
  </>;
}
