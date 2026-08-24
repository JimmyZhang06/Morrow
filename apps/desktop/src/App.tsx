import {
  Archive,
  ArrowLeft,
  ArrowRight,
  BookOpenText,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleHelp,
  Clock3,
  CloudOff,
  Command,
  Eye,
  EyeOff,
  Feather,
  FileClock,
  Footprints,
  Gauge,
  HeartHandshake,
  History,
  House,
  Info,
  KeyRound,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  Maximize2,
  Menu,
  Minus,
  NotebookPen,
  PanelLeftClose,
  PanelLeftOpen,
  PenLine,
  Plus,
  Quote,
  RefreshCw,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
  Undo2,
  Wifi,
  X,
  XCircle,
} from "lucide-react";
import {
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  appendEntryRevision,
  checkHealth,
  checkReadiness,
  createAction,
  createEntry,
  deleteEntry,
  generateCandidateInsight,
  getAction,
  getCapabilities,
  getEntry,
  getEvidenceExcerpt,
  getMemoryDetail,
  listEntries,
  listMemoryInbox,
  safeApiMessage,
  submitActionVerdict,
  submitMemoryVerdict,
} from "./api";
import type {
  ActionResource,
  ActionVerdictType,
  ApiSettings,
  BackendCapabilities,
  BackendState,
  Entry,
  LocalAction,
  MemoryClaimKind,
  MemoryDetail,
  MemoryInboxItem,
  VerdictType,
} from "./types";

type View = "today" | "records" | "insights" | "actions" | "settings";
type EntryFilter = "all" | "processing" | "local";
type InsightFilter = "pending" | "confirmed" | "history";

const storage = {
  apiUrl: "vistora.apiBaseUrl",
  vaultId: "vistora.vaultId",
  candidateRequestKeys: "vistora.candidateRequestKeys",
  localEntries: "vistora.localEntries",
  draft: "vistora.recordDraft",
  localActions: "vistora.localActions.v2",
  sidebarCollapsed: "vistora.sidebarCollapsed",
  privacyMask: "vistora.privacyMask",
  hidePreview: "vistora.hidePreview",
};

const EMPTY_CAPABILITIES: BackendCapabilities = {
  entries: false,
  entry_revisions: false,
  entry_deletion: false,
  memory_review: false,
  memory_verdicts: false,
  candidate_insights: false,
  actions: false,
  model_run_receipts: false,
};

const SAMPLE_INSIGHT: MemoryInboxItem = {
  memory_id: "sample-insight",
  kind: "pattern_hypothesis",
  support_count: 2,
  counterevidence_count: 1,
  current_verdict: null,
  etag: '"sample:1"',
  version: {
    derived_object_id: "sample-derived",
    version_no: 1,
    statement: "在需要公开表达意见时，你可能会先削弱自己的判断。",
    structured_payload: {},
    epistemic_type: "inferred",
    attribution: "model_hypothesis",
    uncertainty: "这只是一种可能解释，需要由你判断。",
    state: "candidate",
    confidence_band: "medium",
    origin: "pipeline_derived",
    valid_time: { from: new Date().toISOString(), precision: "unknown" },
    system_time: { from: new Date().toISOString() },
  },
};

const navItems: Array<{ id: View; label: string; icon: typeof House }> = [
  { id: "today", label: "今天", icon: House },
  { id: "records", label: "记录", icon: NotebookPen },
  { id: "insights", label: "认识", icon: Sparkles },
  { id: "actions", label: "行动", icon: Footprints },
];

function useStoredState<T>(key: string, fallback: T): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : (JSON.parse(raw) as T);
    } catch {
      return fallback;
    }
  });
  useEffect(() => {
    localStorage.setItem(key, JSON.stringify(value));
  }, [key, value]);
  return [value, setValue];
}

function timeLabel(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function fullDateLabel(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function relativeDayLabel(value: string) {
  const date = new Date(value);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const difference = Math.round((today - target) / 86_400_000);
  if (difference === 0) return "今天";
  if (difference === 1) return "昨天";
  return new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric" }).format(date);
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

function kindLabel(kind: MemoryClaimKind) {
  const labels: Record<MemoryClaimKind, string> = {
    explicit_fact: "明确事实",
    preference: "偏好",
    value: "价值取向",
    goal: "目标",
    relationship: "关系线索",
    self_description: "自我描述",
    pattern_hypothesis: "模式假设",
  };
  return labels[kind];
}

function processingLabel(entry: Entry) {
  if (entry.syncState === "syncing") return "等待后台整理";
  if (entry.syncState === "local" || entry.syncState === "failed") return "尚未整理";
  return {
    pending: "正在整理",
    ready: "已整理",
    partial: "部分完成",
    failed: "整理失败",
    delayed: "稍后整理",
  }[entry.processing.state];
}

function mergeEntries(remote: Entry[], local: Entry[]) {
  const result = new Map<string, Entry>();
  remote.forEach((entry) => result.set(entry.id, { ...entry, syncState: "synced" }));
  local.forEach((entry) => result.set(entry.id, entry));
  return [...result.values()].sort(
    (left, right) => new Date(right.captured_at).getTime() - new Date(left.captured_at).getTime(),
  );
}

function actionFromApi(resource: ActionResource, fallback?: MemoryInboxItem): LocalAction {
  const now = new Date().toISOString();
  const id = resource.action_id || resource.id;
  if (!id) throw new Error("action response is missing its identity");
  return {
    id,
    title: resource.title,
    note: [resource.description, resource.rationale].filter(Boolean).join("\n"),
    durationMinutes: resource.estimated_minutes || 5,
    context: resource.exit_plan || "你可以随时停止，不产生外部副作用",
    sourceMemoryId: resource.source_memory_id || resource.memory_id || fallback?.memory_id,
    sourceStatement: resource.source_statement || fallback?.version.statement,
    state: resource.state === "proposed" ? "candidate" : resource.state,
    createdAt: resource.created_at || now,
    updatedAt: resource.updated_at || now,
    remote: true,
  };
}

function operationRequestKey(requestId: string) {
  try {
    const raw = localStorage.getItem(storage.candidateRequestKeys);
    const keys = raw ? JSON.parse(raw) as Record<string, string> : {};
    if (keys[requestId]) return keys[requestId];
    const key = crypto.randomUUID();
    localStorage.setItem(storage.candidateRequestKeys, JSON.stringify({ ...keys, [requestId]: key }));
    return key;
  } catch {
    return crypto.randomUUID();
  }
}

function candidateRequestKey(entry: Entry) {
  return operationRequestKey(`candidate:${entry.id}:${entry.revision}`);
}

function App() {
  const [view, setView] = useState<View>("today");
  const [settings, setSettings] = useState<ApiSettings>(() => ({
    baseUrl: localStorage.getItem(storage.apiUrl) || import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000",
    token: import.meta.env.VITE_DEV_AUTH_TOKEN || "",
    vaultId: localStorage.getItem(storage.vaultId) || import.meta.env.VITE_VAULT_ID || "",
  }));
  const [backend, setBackend] = useState<BackendState>({
    phase: "checking",
    ready: null,
    serverVersion: null,
    capabilities: EMPTY_CAPABILITIES,
    lastCheckedAt: null,
    message: null,
  });
  const [remoteEntries, setRemoteEntries] = useState<Entry[]>([]);
  const [localEntries, setLocalEntries] = useStoredState<Entry[]>(storage.localEntries, []);
  const [memoryInbox, setMemoryInbox] = useState<MemoryInboxItem[]>([]);
  const [memoryDetail, setMemoryDetail] = useState<MemoryDetail | null>(null);
  const [actions, setActions] = useStoredState<LocalAction[]>(storage.localActions, []);
  const [draft, setDraft] = useStoredState(storage.draft, "");
  const [sidebarCollapsed, setSidebarCollapsed] = useStoredState(storage.sidebarCollapsed, false);
  const [privacyMask, setPrivacyMask] = useStoredState(storage.privacyMask, false);
  const [hidePreview, setHidePreview] = useStoredState(storage.hidePreview, true);
  const [composerOpen, setComposerOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null);
  const [selectedMemoryId, setSelectedMemoryId] = useState<string | null>(null);
  const [showSampleInsight, setShowSampleInsight] = useState(false);
  const [editTarget, setEditTarget] = useState<Entry | null>(null);
  const [editText, setEditText] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Entry | null>(null);
  const [correctionTarget, setCorrectionTarget] = useState<MemoryInboxItem | null>(null);
  const [correctionText, setCorrectionText] = useState("");
  const [actionEditorOpen, setActionEditorOpen] = useState(false);
  const [actionDraft, setActionDraft] = useState({
    title: "",
    note: "",
    durationMinutes: 5,
    context: "下一次类似情境",
    sourceMemoryId: "",
    sourceStatement: "",
  });
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [appVersion, setAppVersion] = useState("0.4.4");
  const homeComposerRef = useRef<HTMLTextAreaElement>(null);

  const entries = useMemo(
    () => mergeEntries(remoteEntries, localEntries),
    [localEntries, remoteEntries],
  );
  const selectedEntry = entries.find((entry) => entry.id === selectedEntryId) || null;
  const pendingSyncCount = localEntries.filter((entry) =>
    ["local", "failed"].includes(entry.syncState || ""),
  ).length;
  const pendingInsightCount = memoryInbox.filter((item) => !item.current_verdict).length;
  const activeActionCount = actions.filter((action) => action.state === "accepted").length;

  const refreshBackend = useCallback(
    async (activeSettings = settings, quiet = false) => {
      if (!quiet) setBackend((current) => ({ ...current, phase: "checking", message: null }));
      const live = await checkHealth(activeSettings);
      if (!live.ok) {
        setBackend({
          phase: "offline",
          ready: null,
          serverVersion: null,
          capabilities: EMPTY_CAPABILITIES,
          lastCheckedAt: new Date().toISOString(),
          message: "无法连接后端；本机记录仍可继续使用。",
        });
        return;
      }

      const [ready, capabilities, entryPage, memoryPage] = await Promise.all([
        checkReadiness(activeSettings),
        getCapabilities(activeSettings),
        listEntries(activeSettings),
        listMemoryInbox(activeSettings),
      ]);

      const inferredCapabilities: BackendCapabilities = capabilities.ok
        ? capabilities.data.features
        : {
            ...EMPTY_CAPABILITIES,
            entries: entryPage.ok,
            entry_revisions: entryPage.ok,
            entry_deletion: entryPage.ok,
            memory_review: memoryPage.ok,
            memory_verdicts: memoryPage.ok,
          };

      if (entryPage.ok && Array.isArray(entryPage.data.items)) {
        setRemoteEntries(entryPage.data.items.map((entry) => ({ ...entry, syncState: "synced" })));
      }
      if (memoryPage.ok && Array.isArray(memoryPage.data.items)) {
        setMemoryInbox(memoryPage.data.items);
      }

      setBackend({
        phase: "online",
        ready: ready.ok,
        serverVersion: capabilities.ok ? capabilities.data.server_version : null,
        capabilities: inferredCapabilities,
        lastCheckedAt: new Date().toISOString(),
        message: ready.ok ? null : "服务已启动，但数据库或依赖尚未就绪。",
      });
    },
    [settings],
  );

  useEffect(() => {
    void refreshBackend();
    const timer = window.setInterval(() => void refreshBackend(settings, true), 60_000);
    return () => window.clearInterval(timer);
  }, [refreshBackend, settings]);

  useEffect(() => {
    if (window.vistoraDesktop) void window.vistoraDesktop.appVersion().then(setAppVersion);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      if (event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
      if (event.key.toLowerCase() === "n") {
        event.preventDefault();
        setComposerOpen(true);
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, []);

  const saveRecord = async (content: string) => {
    const normalized = content.trim();
    if (!normalized || busy) return;
    setBusy("save-record");
    const clientId = crypto.randomUUID();
    const now = new Date().toISOString();
    const optimistic: Entry = {
      id: clientId,
      content: normalized,
      captured_at: now,
      created_at: now,
      revision: 1,
      source_type: "note",
      data_class: "sensitive",
      processing: { state: "pending" },
      revisions: [{
        id: `${clientId}:1`,
        revision: 1,
        created_at: now,
        content_mime: "text/plain; charset=utf-8",
        content: normalized,
      }],
      syncState: backend.capabilities.entries ? "syncing" : "local",
    };
    setLocalEntries((current) => [optimistic, ...current]);
    setDraft("");
    setComposerOpen(false);
    setToast(
      backend.capabilities.entries
        ? "记录已先保存在本机，正在同步"
        : "记录已保存在本机；后端记录服务接通后可同步",
    );

    if (backend.capabilities.entries) {
      const result = await createEntry(settings, normalized, clientId, now);
      if (result.ok) {
        const synced: Entry = {
          ...optimistic,
          id: result.data.id,
          revision: result.data.revision,
          processing: result.data.processing,
          syncState: "synced",
        };
        setLocalEntries((current) => current.filter((entry) => entry.id !== clientId));
        setRemoteEntries((current) => [
          synced,
          ...current.filter((entry) => entry.id !== result.data.id),
        ]);
      } else {
        setLocalEntries((current) =>
          current.map((entry) =>
            entry.id === clientId ? { ...entry, syncState: "failed" } : entry,
          ),
        );
      }
      setToast(
        result.ok
          ? "记录已同步；后台整理不会阻塞你继续记录"
          : safeApiMessage(result, "同步暂时失败，记录仍安全保存在本机"),
      );
    }
    setBusy(null);
  };

  const retrySync = async () => {
    const pending = localEntries.filter((entry) => ["local", "failed"].includes(entry.syncState || ""));
    if (!backend.capabilities.entries) {
      setToast("记录 API 尚未接入，暂时无法同步");
      return;
    }
    if (!pending.length) {
      setToast("没有等待同步的记录");
      return;
    }
    setBusy("sync");
    let remaining = 0;
    for (const entry of pending) {
      const result = await createEntry(settings, entry.content, entry.id, entry.captured_at);
      if (result.ok) {
        const synced: Entry = {
          ...entry,
          id: result.data.id,
          revision: result.data.revision,
          processing: result.data.processing,
          syncState: "synced",
        };
        setLocalEntries((current) => current.filter((item) => item.id !== entry.id));
        setRemoteEntries((current) => [
          synced,
          ...current.filter((item) => item.id !== result.data.id),
        ]);
      } else {
        setLocalEntries((current) =>
          current.map((item) =>
            item.id === entry.id ? { ...item, syncState: "failed" } : item,
          ),
        );
      }
      if (!result.ok) remaining += 1;
    }
    setBusy(null);
    setToast(remaining ? `${remaining} 条记录仍等待同步` : "本机记录已全部同步");
    if (!remaining) void refreshBackend(settings, true);
  };

  const openEditEntry = (entry: Entry) => {
    setEditTarget(entry);
    setEditText(entry.content);
  };

  const saveEntryRevision = async () => {
    if (!editTarget || !editText.trim()) return;
    setBusy("edit-entry");
    const isLocal = editTarget.syncState !== "synced";
    if (isLocal) {
      setLocalEntries((current) =>
        current.map((entry) =>
          entry.id === editTarget.id
            ? (() => {
                const revisions = entry.revisions?.length
                  ? entry.revisions
                  : [{
                      id: `${entry.id}:${entry.revision}`,
                      revision: entry.revision,
                      created_at: entry.created_at,
                      content_mime: "text/plain; charset=utf-8",
                      content: entry.content,
                    }];
                const nextRevision = entry.revision + 1;
                return {
                  ...entry,
                  content: editText.trim(),
                  revision: nextRevision,
                  revisions: [...revisions, {
                    id: `${entry.id}:${nextRevision}`,
                    revision: nextRevision,
                    created_at: new Date().toISOString(),
                    content_mime: "text/plain; charset=utf-8",
                    content: editText.trim(),
                  }],
                  processing: { state: "pending" as const },
                };
              })()
            : entry,
        ),
      );
      setEditTarget(null);
      setToast("已创建本机修订，旧版正文仍可在修订历史中查看");
      setBusy(null);
      return;
    }
    if (!backend.capabilities.entry_revisions) {
      setToast("当前后端尚未开放记录修订接口");
      setBusy(null);
      return;
    }
    const result = await appendEntryRevision(
      settings,
      editTarget.id,
      editText.trim(),
      editTarget.revision,
    );
    if (result.ok) {
      const detail = await getEntry(settings, editTarget.id);
      setRemoteEntries((current) => current.map((entry) =>
        entry.id === editTarget.id
          ? detail.ok
            ? { ...detail.data, syncState: "synced" }
            : {
                ...entry,
                content: editText.trim(),
                revision: result.data.revision,
                processing: result.data.processing,
              }
          : entry,
      ));
      setEditTarget(null);
      setToast("新修订已保存，原始版本没有被覆盖");
    } else if (result.status === 409) {
      setToast("这条记录已在其他位置改变，请刷新后再修改");
      void refreshBackend(settings, true);
    } else {
      setToast(safeApiMessage(result, "暂时无法保存修订"));
    }
    setBusy(null);
  };

  const confirmDeleteEntry = async () => {
    if (!deleteTarget) return;
    setBusy("delete-entry");
    if (deleteTarget.syncState !== "synced") {
      setLocalEntries((current) => current.filter((entry) => entry.id !== deleteTarget.id));
      setSelectedEntryId(null);
      setDeleteTarget(null);
      setToast("本机记录已删除");
      setBusy(null);
      return;
    }
    if (!backend.capabilities.entry_deletion) {
      setToast("当前后端尚未开放记录删除接口");
      setBusy(null);
      return;
    }
    const result = await deleteEntry(settings, deleteTarget.id, deleteTarget.revision);
    if (result.ok) {
      setRemoteEntries((current) => current.filter((entry) => entry.id !== deleteTarget.id));
      setLocalEntries((current) => current.filter((entry) => entry.id !== deleteTarget.id));
      setSelectedEntryId(null);
      setDeleteTarget(null);
      setToast("记录已隔离，相关派生内容将由后台重新检查");
    } else if (result.status === 409) {
      setToast("记录版本已变化，请刷新后重新确认删除");
    } else {
      setToast(safeApiMessage(result, "暂时无法删除这条记录"));
    }
    setBusy(null);
  };

  const selectMemory = async (item: MemoryInboxItem) => {
    setSelectedMemoryId(item.memory_id);
    setMemoryDetail(null);
    if (!backend.capabilities.memory_review) return;
    setBusy("memory-detail");
    const result = await getMemoryDetail(settings, item.memory_id);
    if (result.ok) {
      const etag = result.headers.etag || result.data.etag || item.etag;
      setMemoryDetail({ ...result.data, etag });
      setMemoryInbox((current) => current.map((memory) => memory.memory_id === item.memory_id
        ? {
            ...memory,
            kind: result.data.kind,
            version: result.data.version,
            current_verdict: result.data.current_verdict,
            etag,
          }
        : memory));
    }
    else setToast(safeApiMessage(result, "暂时无法读取这条认识"));
    setBusy(null);
  };

  const applyVerdict = async (
    item: MemoryInboxItem,
    verdict: VerdictType,
    correction?: string,
  ) => {
    if (!backend.capabilities.memory_verdicts) {
      setToast("认识裁定 API 尚未接入，当前不会伪造成功状态");
      return;
    }
    if (verdict === "correct" && !correction?.trim()) return;
    const etag = memoryDetail?.memory_id === item.memory_id && memoryDetail.etag
      ? memoryDetail.etag
      : item.etag;
    setBusy("verdict");
    const result = await submitMemoryVerdict(settings, item.memory_id, etag, verdict, correction);
    if (result.ok) {
      setMemoryInbox((current) =>
        current.map((memory) =>
          memory.memory_id === item.memory_id
            ? { ...memory, current_verdict: verdict, etag: result.data.etag }
            : memory,
        ),
      );
      setCorrectionTarget(null);
      setCorrectionText("");
      setToast(
        verdict === "confirm"
          ? "已记录为你当前认可的理解，之后仍可修改"
          : verdict === "correct"
            ? "你的版本已成为主版本"
            : verdict === "reject"
              ? "已记下：这不符合你的情况"
              : "已暂缓，不会持续催促你",
      );
      if (verdict === "confirm" || verdict === "correct") void selectMemory({ ...item, etag: result.data.etag });
    } else if (result.status === 409) {
      setToast("这条认识已经变化，已重新读取最新版本");
      void refreshBackend(settings, true);
    } else {
      setToast(safeApiMessage(result, "暂时无法保存你的判断"));
    }
    setBusy(null);
  };

  const openCorrection = (item: MemoryInboxItem) => {
    setCorrectionTarget(item);
    setCorrectionText("");
  };

  const selectEntry = async (entryId: string) => {
    setSelectedEntryId(entryId);
    const entry = entries.find((item) => item.id === entryId);
    if (!entry || entry.syncState !== "synced" || !backend.capabilities.entries) return;
    const result = await getEntry(settings, entryId);
    if (result.ok) {
      setRemoteEntries((current) => current.map((item) =>
        item.id === entryId ? { ...result.data, syncState: "synced" } : item,
      ));
    } else {
      setToast(safeApiMessage(result, "暂时无法读取记录详情"));
    }
  };

  const generateInsight = async (entry: Entry) => {
    if (entry.syncState !== "synced") {
      setToast("请先把记录同步到后端，再生成候选认识");
      return;
    }
    if (!backend.capabilities.candidate_insights) {
      setToast("当前后端尚未开放候选认识生成接口");
      return;
    }
    setBusy("generate-insight");
    const result = await generateCandidateInsight(
      settings,
      entry.id,
      entry.revision,
      candidateRequestKey(entry),
    );
    if (result.ok && result.data.status === "succeeded" && result.data.memory_id) {
      const page = await listMemoryInbox(settings);
      if (page.ok) {
        setMemoryInbox(page.data.items);
        const generated = page.data.items.find((item) => item.memory_id === result.data.memory_id);
        setView("insights");
        if (generated) await selectMemory(generated);
        else {
          setSelectedMemoryId(result.data.memory_id);
          const detail = await getMemoryDetail(settings, result.data.memory_id);
          if (detail.ok) setMemoryDetail({ ...detail.data, etag: detail.headers.etag || detail.data.etag });
        }
      }
      setToast("候选认识已生成，请查看真实依据后再判断");
    } else if (result.ok && result.data.status === "processing") {
      setToast("候选认识仍在生成；再次点击会安全续查同一次请求");
    } else {
      setToast(safeApiMessage(result, "候选认识生成失败，没有保存不完整结果"));
    }
    setBusy(null);
  };

  const openActionEditor = async (memory?: MemoryInboxItem) => {
    if (memory && backend.capabilities.actions) {
      setBusy("create-action");
      const result = await createAction(
        settings,
        memory.memory_id,
        operationRequestKey(`action:create:${memory.memory_id}:${memory.version.derived_object_id}`),
      );
      if (result.ok) {
        try {
          const action = actionFromApi(result.data, memory);
          if (result.headers.etag) action.etag = result.headers.etag;
          setActions((current) => [action, ...current.filter((item) => item.id !== action.id)]);
          setView("actions");
          setToast("小行动候选已创建；只有你接受后才会成为行动");
        } catch {
          setToast("后端返回的行动缺少必要字段，未写入本机状态");
        }
      } else {
        setToast(safeApiMessage(result, "暂时无法从这条认识创建小行动"));
      }
      setBusy(null);
      return;
    }
    setActionDraft({
      title: "",
      note: memory ? "基于一条你已经确认或修正的认识，由你决定具体做法。" : "",
      durationMinutes: 5,
      context: "下一次类似情境",
      sourceMemoryId: memory?.memory_id || "",
      sourceStatement: memory?.version.statement || "",
    });
    setActionEditorOpen(true);
  };

  const saveAction = () => {
    if (!actionDraft.title.trim()) return;
    const now = new Date().toISOString();
    setActions((current) => [
      {
        id: crypto.randomUUID(),
        title: actionDraft.title.trim(),
        note: actionDraft.note.trim(),
        durationMinutes: actionDraft.durationMinutes,
        context: actionDraft.context.trim(),
        sourceMemoryId: actionDraft.sourceMemoryId || undefined,
        sourceStatement: actionDraft.sourceStatement || undefined,
        state: "candidate",
        createdAt: now,
        updatedAt: now,
      },
      ...current,
    ]);
    setActionEditorOpen(false);
    setView("actions");
    setToast("行动候选已保存在本机；只有你接受后才会成为行动");
  };

  const updateAction = async (id: string, patch: Partial<LocalAction>) => {
    const currentAction = actions.find((action) => action.id === id);
    const targetState = patch.state;
    const verdict: ActionVerdictType | null = targetState === "accepted"
      ? "accept"
      : targetState === "completed"
        ? "complete"
        : targetState === "revoked"
          ? "revoke"
          : null;
    if (currentAction?.remote && verdict) {
      setBusy(`action:${id}`);
      const result = await submitActionVerdict(
        settings,
        id,
        verdict,
        currentAction.etag,
        operationRequestKey(`action:verdict:${id}:${currentAction.etag || "unknown"}:${verdict}`),
      );
      if (result.ok) {
        const latest = await getAction(settings, id);
        if (latest.ok) {
          try {
            const next = actionFromApi(latest.data);
            next.sourceStatement = next.sourceStatement || currentAction.sourceStatement;
            next.etag = latest.headers.etag || result.headers.etag || currentAction.etag;
            setActions((items) => items.map((action) => action.id === id ? next : action));
            setToast(verdict === "accept" ? "行动已接受" : verdict === "complete" ? "结果已记下" : "行动已撤销");
          } catch {
            setToast("后端返回的行动状态无效，已保留当前界面状态");
          }
        } else {
          setActions((items) => items.map((action) => action.id === id ? { ...action, state: result.data.state, etag: result.headers.etag || action.etag, updatedAt: result.data.updated_at } : action));
          setToast(verdict === "accept" ? "行动已接受" : verdict === "complete" ? "结果已记下" : "行动已撤销");
        }
      } else if (result.status === 409) {
        setToast("行动状态已经变化，请刷新后再试");
      } else {
        setToast(safeApiMessage(result, "暂时无法更新行动"));
      }
      setBusy(null);
      return;
    }
    setActions((current) =>
      current.map((action) =>
        action.id === id ? { ...action, ...patch, updatedAt: new Date().toISOString() } : action,
      ),
    );
  };

  const saveApiSettings = async (next: ApiSettings) => {
    const normalized = next.baseUrl.trim().replace(/\/$/, "") || "http://127.0.0.1:8000";
    const active = { ...next, baseUrl: normalized };
    localStorage.setItem(storage.apiUrl, normalized);
    localStorage.setItem(storage.vaultId, active.vaultId.trim());
    setSettings(active);
    await refreshBackend(active);
    setToast("连接设置已保存并重新检测");
  };

  const navigate = (next: View) => {
    setView(next);
    setSelectedEntryId(null);
    setSelectedMemoryId(null);
    setMemoryDetail(null);
  };

  const searchResults = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    if (!query) return { entries: [] as Entry[], memories: [] as MemoryInboxItem[], actions: [] as LocalAction[] };
    return {
      entries: entries.filter((entry) => entry.content.toLocaleLowerCase().includes(query)).slice(0, 5),
      memories: memoryInbox.filter((memory) => memory.version.statement.toLocaleLowerCase().includes(query)).slice(0, 5),
      actions: actions.filter((action) => `${action.title} ${action.note}`.toLocaleLowerCase().includes(query)).slice(0, 5),
    };
  }, [actions, entries, memoryInbox, searchQuery]);

  const viewTitle = {
    today: "今天",
    records: "记录",
    insights: "认识",
    actions: "行动",
    settings: "设置",
  }[view];

  return (
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <header className="native-titlebar">
        <div className="titlebar-drag"><span className="tiny-mark"><Feather /></span><span>Vistora</span></div>
        <div className="window-controls" aria-label="窗口控制">
          <button aria-label="最小化" onClick={() => window.vistoraDesktop?.window.minimize()}><Minus /></button>
          <button aria-label="最大化" onClick={() => window.vistoraDesktop?.window.toggleMaximize()}><Maximize2 /></button>
          <button className="window-close" aria-label="关闭" onClick={() => window.vistoraDesktop?.window.close()}><X /></button>
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <div className="sidebar-top">
            <button className="sidebar-toggle" aria-label={sidebarCollapsed ? "展开侧栏" : "收起侧栏"} onClick={() => setSidebarCollapsed((value) => !value)}>
              {sidebarCollapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
            </button>
            <button className="new-record-button" onClick={() => setComposerOpen(true)} title="新记录 (Ctrl+N)">
              <span className="new-record-icon"><PenLine /></span><span>新记录</span>
            </button>
          </div>
          <nav className="primary-nav" aria-label="主导航">
            {navItems.map((item) => {
              const Icon = item.icon;
              const count = item.id === "insights" ? pendingInsightCount : item.id === "actions" ? activeActionCount : 0;
              return (
                <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => navigate(item.id)} title={item.label}>
                  <Icon /><span>{item.label}</span>{count > 0 && <em>{count}</em>}
                </button>
              );
            })}
          </nav>

          <div className="sidebar-section">
            <div className="sidebar-section-title"><span>最近记录</span><button onClick={() => navigate("records")} aria-label="查看全部记录"><ChevronRight /></button></div>
            <div className="recent-list">
              {entries.slice(0, 4).map((entry) => (
                <button key={entry.id} onClick={() => { setView("records"); void selectEntry(entry.id); }}>
                  <span>{entry.content}</span><small>{relativeDayLabel(entry.captured_at)}</small>
                </button>
              ))}
              {!entries.length && <p>你写下的内容会安静地出现在这里。</p>}
            </div>
          </div>

          <div className="sidebar-bottom">
            <button className={`settings-nav ${view === "settings" ? "active" : ""}`} onClick={() => navigate("settings")} title="设置与隐私">
              <Settings /><span>设置与隐私</span>
            </button>
            <BackendBadge backend={backend} collapsed={sidebarCollapsed} onRefresh={() => void refreshBackend()} />
          </div>
        </aside>

        <section className="stage">
          <header className="stage-header">
            <div className="stage-title"><button className="mobile-menu" onClick={() => setSidebarCollapsed((value) => !value)} aria-label="切换侧栏"><Menu /></button><span>{viewTitle}</span></div>
            <div className="stage-tools">
              <button className="search-trigger" onClick={() => setSearchOpen(true)}><Search /><span>搜索</span><kbd>Ctrl K</kbd></button>
              <button
                className={`privacy-button ${privacyMask ? "active" : ""}`}
                title={privacyMask ? "关闭隐私遮罩" : "开启隐私遮罩"}
                aria-label={privacyMask ? "关闭隐私遮罩" : "开启隐私遮罩"}
                aria-pressed={privacyMask}
                onClick={() => setPrivacyMask((value) => !value)}
              >
                {privacyMask ? <EyeOff /> : <LockKeyhole />}
              </button>
            </div>
          </header>
          <main className={`stage-content ${privacyMask ? "is-masked" : ""}`}>
            {view === "today" && (
              <TodayView
                draft={draft}
                setDraft={setDraft}
                onSave={() => void saveRecord(draft)}
                composerRef={homeComposerRef}
                busy={busy === "save-record"}
                entries={entries}
                pendingSyncCount={pendingSyncCount}
                pendingInsightCount={pendingInsightCount}
                activeActionCount={activeActionCount}
                onNavigate={navigate}
                onOpenEntry={(entryId) => { setView("records"); void selectEntry(entryId); }}
                onRetrySync={() => void retrySync()}
                backend={backend}
              />
            )}
            {view === "records" && (
              <RecordsView
                entries={entries}
                selectedEntry={selectedEntry}
                onSelect={(entryId) => void selectEntry(entryId)}
                onBack={() => setSelectedEntryId(null)}
                onNew={() => setComposerOpen(true)}
                onEdit={openEditEntry}
                onDelete={setDeleteTarget}
                onGenerateInsight={(entry) => void generateInsight(entry)}
                canGenerateInsight={backend.capabilities.candidate_insights}
                generatingInsight={busy === "generate-insight"}
                pendingSyncCount={pendingSyncCount}
                onRetrySync={() => void retrySync()}
              />
            )}
            {view === "insights" && (
              <InsightsView
                backend={backend}
                settings={settings}
                items={memoryInbox}
                selectedId={selectedMemoryId}
                detail={memoryDetail}
                busy={busy}
                showSample={showSampleInsight}
                onToggleSample={setShowSampleInsight}
                onSelect={(item) => void selectMemory(item)}
                onBack={() => { setSelectedMemoryId(null); setMemoryDetail(null); }}
                onVerdict={(item, verdict) => void applyVerdict(item, verdict)}
                onCorrect={openCorrection}
                onCreateAction={(item) => void openActionEditor(item)}
              />
            )}
            {view === "actions" && (
              <ActionsView
                actions={actions}
                backend={backend}
                onCreate={() => void openActionEditor()}
                onUpdate={(id, patch) => void updateAction(id, patch)}
                busy={busy}
              />
            )}
            {view === "settings" && (
              <SettingsView
                settings={settings}
                backend={backend}
                appVersion={appVersion}
                hidePreview={hidePreview}
                setHidePreview={setHidePreview}
                privacyMask={privacyMask}
                setPrivacyMask={setPrivacyMask}
                onSave={saveApiSettings}
                onRefresh={() => void refreshBackend()}
              />
            )}
          </main>
        </section>
      </div>

      {composerOpen && (
        <ComposerModal draft={draft} setDraft={setDraft} busy={busy === "save-record"} onClose={() => setComposerOpen(false)} onSave={() => void saveRecord(draft)} />
      )}
      {searchOpen && (
        <SearchPalette
          query={searchQuery}
          setQuery={setSearchQuery}
          results={searchResults}
          onClose={() => { setSearchOpen(false); setSearchQuery(""); }}
          onEntry={(entry) => { setSearchOpen(false); setView("records"); void selectEntry(entry.id); }}
          onMemory={(memory) => { setSearchOpen(false); setView("insights"); void selectMemory(memory); }}
          onAction={() => { setSearchOpen(false); setView("actions"); }}
        />
      )}
      {editTarget && (
        <EditEntryModal entry={editTarget} value={editText} setValue={setEditText} busy={busy === "edit-entry"} onClose={() => setEditTarget(null)} onSave={() => void saveEntryRevision()} />
      )}
      {deleteTarget && (
        <DeleteEntryDialog entry={deleteTarget} busy={busy === "delete-entry"} onClose={() => setDeleteTarget(null)} onConfirm={() => void confirmDeleteEntry()} />
      )}
      {correctionTarget && (
        <CorrectionModal item={correctionTarget} value={correctionText} setValue={setCorrectionText} busy={busy === "verdict"} onClose={() => setCorrectionTarget(null)} onSave={() => void applyVerdict(correctionTarget, "correct", correctionText)} />
      )}
      {actionEditorOpen && (
        <ActionEditor value={actionDraft} setValue={setActionDraft} onClose={() => setActionEditorOpen(false)} onSave={saveAction} />
      )}
      {toast && <div className="toast" role="status"><CheckCircle2 />{toast}</div>}
    </div>
  );
}

function BackendBadge({ backend, collapsed, onRefresh }: { backend: BackendState; collapsed: boolean; onRefresh: () => void }) {
  const online = backend.phase === "online";
  const label = backend.phase === "checking" ? "正在检查" : online ? (backend.ready ? "服务已就绪" : "服务已连接") : "本机模式";
  return (
    <button className={`backend-badge ${backend.phase}`} onClick={onRefresh} title={collapsed ? label : "重新检查后端"}>
      {backend.phase === "checking" ? <LoaderCircle className="spin" /> : online ? <Wifi /> : <CloudOff />}
      <span><strong>{label}</strong><small>{online ? `${Object.values(backend.capabilities).filter(Boolean).length} 项能力可用` : "记录仍会保存在本机"}</small></span>
      {!collapsed && <RefreshCw className="refresh-icon" />}
    </button>
  );
}

type TodayProps = {
  draft: string;
  setDraft: (value: string) => void;
  onSave: () => void;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  busy: boolean;
  entries: Entry[];
  pendingSyncCount: number;
  pendingInsightCount: number;
  activeActionCount: number;
  onNavigate: (view: View) => void;
  onOpenEntry: (entryId: string) => void;
  onRetrySync: () => void;
  backend: BackendState;
};

function TodayView(props: TodayProps) {
  return (
    <div className="today-page page-enter">
      <section className="welcome-block">
        <p className="eyebrow">{new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" }).format(new Date())}</p>
        <h1>{greeting()}，此刻有什么值得留下？</h1>
        <p>不需要写完整。先保留原话，整理可以稍后发生。</p>
      </section>
      <InlineComposer ref={props.composerRef} value={props.draft} setValue={props.setDraft} onSave={props.onSave} busy={props.busy} />
      <div className="starter-prompts" aria-label="记录提示">
        <button onClick={() => { props.setDraft("今天有一个瞬间让我停了一下："); props.composerRef.current?.focus(); }}>一个停顿的瞬间</button>
        <button onClick={() => { props.setDraft("最近反复出现的一个想法是："); props.composerRef.current?.focus(); }}>反复出现的想法</button>
        <button onClick={() => { props.setDraft("如果只保留一句原话，我想记下："); props.composerRef.current?.focus(); }}>只留一句原话</button>
      </div>

      {(props.pendingSyncCount > 0 || props.pendingInsightCount > 0 || props.activeActionCount > 0) && (
        <section className="attention-strip">
          {props.pendingSyncCount > 0 && <button onClick={props.onRetrySync}><CloudOff /><span><strong>{props.pendingSyncCount}</strong> 条等待同步</span><ArrowRight /></button>}
          {props.pendingInsightCount > 0 && <button onClick={() => props.onNavigate("insights")}><Sparkles /><span><strong>{props.pendingInsightCount}</strong> 条认识等你判断</span><ArrowRight /></button>}
          {props.activeActionCount > 0 && <button onClick={() => props.onNavigate("actions")}><Footprints /><span><strong>{props.activeActionCount}</strong> 个正在尝试</span><ArrowRight /></button>}
        </section>
      )}

      <section className="recent-section">
        <div className="section-heading"><div><span>最近留下的</span><small>{props.entries.length ? "你的原话始终是这里的主体" : "从一句话开始就够了"}</small></div>{props.entries.length > 0 && <button onClick={() => props.onNavigate("records")}>查看全部 <ArrowRight /></button>}</div>
        {props.entries.length ? (
          <div className="home-records">
            {props.entries.slice(0, 3).map((entry) => (
              <button key={entry.id} onClick={() => props.onOpenEntry(entry.id)}>
                <p>{entry.content}</p><span>{relativeDayLabel(entry.captured_at)} · {timeLabel(entry.captured_at)}</span><ChevronRight />
              </button>
            ))}
          </div>
        ) : (
          <div className="principle-row">
            <div><span className="principle-icon"><Quote /></span><strong>先保留原话</strong><p>系统不会静默改写你当时记录的内容。</p></div>
            <div><span className="principle-icon"><Layers3 /></span><strong>认识带有依据</strong><p>候选解释与原始记录始终分层展示。</p></div>
            <div><span className="principle-icon"><ShieldCheck /></span><strong>最后由你判断</strong><p>确认、纠正、驳回或暂缓都由你决定。</p></div>
          </div>
        )}
      </section>
      {props.backend.message && <div className="soft-notice"><Info /><span>{props.backend.message}</span></div>}
    </div>
  );
}

const InlineComposer = ({ value, setValue, onSave, busy, ref }: { value: string; setValue: (value: string) => void; onSave: () => void; busy: boolean; ref: RefObject<HTMLTextAreaElement | null> }) => (
  <div className="inline-composer">
    <textarea ref={ref} value={value} onChange={(event) => setValue(event.target.value)} placeholder="写下一句话、一个感受，或刚刚发生的片段……" onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") onSave(); }} />
    <div className="composer-footer"><span><LockKeyhole />仅自己可见 · 草稿自动保存在本机</span><button className="send-button" disabled={!value.trim() || busy} onClick={onSave} aria-label="保存记录">{busy ? <LoaderCircle className="spin" /> : <Send />}</button></div>
  </div>
);

function RecordsView({ entries, selectedEntry, onSelect, onBack, onNew, onEdit, onDelete, onGenerateInsight, canGenerateInsight, generatingInsight, pendingSyncCount, onRetrySync }: {
  entries: Entry[];
  selectedEntry: Entry | null;
  onSelect: (id: string) => void;
  onBack: () => void;
  onNew: () => void;
  onEdit: (entry: Entry) => void;
  onDelete: (entry: Entry) => void;
  onGenerateInsight: (entry: Entry) => void;
  canGenerateInsight: boolean;
  generatingInsight: boolean;
  pendingSyncCount: number;
  onRetrySync: () => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<EntryFilter>("all");
  const filtered = entries.filter((entry) => {
    const matchesQuery = entry.content.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
    const matchesFilter = filter === "all" || (filter === "processing" && entry.processing.state === "pending") || (filter === "local" && entry.syncState !== "synced");
    return matchesQuery && matchesFilter;
  });

  if (selectedEntry) {
    return <RecordDetailView entry={selectedEntry} onBack={onBack} onEdit={() => onEdit(selectedEntry)} onDelete={() => onDelete(selectedEntry)} onGenerateInsight={() => onGenerateInsight(selectedEntry)} canGenerateInsight={canGenerateInsight} generatingInsight={generatingInsight} />;
  }
  return (
    <div className="content-page records-page page-enter">
      <PageIntro title="记录" subtitle="这里保存的是你当时写下的原话，而不是系统替你总结的人生。"><button className="primary-action" onClick={onNew}><Plus />记一条</button></PageIntro>
      <div className="list-toolbar">
        <label className="filter-search"><Search /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索记录中的原话" /></label>
        <div className="segmented-control">
          <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部</button>
          <button className={filter === "processing" ? "active" : ""} onClick={() => setFilter("processing")}>整理中</button>
          <button className={filter === "local" ? "active" : ""} onClick={() => setFilter("local")}>待同步</button>
        </div>
      </div>
      {pendingSyncCount > 0 && <button className="sync-callout" onClick={onRetrySync}><CloudOff /><span>{pendingSyncCount} 条记录只保存在本机</span><strong>尝试同步 <ArrowRight /></strong></button>}
      {filtered.length ? (
        <div className="record-timeline">
          {filtered.map((entry, index) => {
            const showDay = index === 0 || relativeDayLabel(filtered[index - 1].captured_at) !== relativeDayLabel(entry.captured_at);
            return (
              <div className="timeline-group" key={entry.id}>
                <div className={`timeline-date${showDay ? "" : " is-continuation"}`} aria-hidden={!showDay}>
                  {showDay ? relativeDayLabel(entry.captured_at) : ""}
                </div>
                <button className="record-list-item" onClick={() => onSelect(entry.id)}>
                  <div className="record-copy">
                    <p>{entry.content}</p>
                    <div className="record-meta">
                      <span>{timeLabel(entry.captured_at)}</span><i />
                      <span>{processingLabel(entry)}</span><i />
                      <span className={entry.syncState === "synced" ? "is-synced" : "is-local"}>{entry.syncState === "synced" ? "已同步" : "仅在本机"}</span>
                    </div>
                  </div>
                  <ChevronRight />
                </button>
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState icon={query || filter !== "all" ? Search : NotebookPen} title={query || filter !== "all" ? "没有符合条件的记录" : "还没有记录"} description={query || filter !== "all" ? "换一个关键词或筛选条件试试。" : "可以从今天反复想到的一件小事开始。"} action={!query && filter === "all" ? <button onClick={onNew}>写下第一条</button> : undefined} />
      )}
    </div>
  );
}

function RecordDetailView({ entry, onBack, onEdit, onDelete, onGenerateInsight, canGenerateInsight, generatingInsight }: { entry: Entry; onBack: () => void; onEdit: () => void; onDelete: () => void; onGenerateInsight: () => void; canGenerateInsight: boolean; generatingInsight: boolean }) {
  return (
    <div className="reading-page page-enter">
      <button className="back-button" onClick={onBack}><ArrowLeft />返回记录</button>
      <header className="reading-header"><div><span>{fullDateLabel(entry.captured_at)}</span><h1>记录详情</h1></div><div className="reading-actions"><button onClick={onEdit}><PenLine />创建修订</button><button className="danger-text" onClick={onDelete}><Trash2 />删除</button></div></header>
      <article className="source-document"><div className="source-label"><Quote />你的原话</div><p>{entry.content}</p></article>
      <section className="record-insight-callout"><div><Sparkles /><span><strong>从这条原话提出一个候选认识</strong><small>模型只能引用这条记录中真实存在的片段；生成结果仍需由你确认、纠正或驳回。</small></span></div><button className="primary-action" disabled={!canGenerateInsight || entry.syncState !== "synced" || generatingInsight} onClick={onGenerateInsight}>{generatingInsight ? <LoaderCircle className="spin" /> : <Sparkles />}{generatingInsight ? "正在生成" : entry.syncState !== "synced" ? "等待同步" : canGenerateInsight ? "生成候选认识" : "服务未接入"}</button></section>
      <div className="record-facts"><div><span>同步状态</span><strong>{entry.syncState === "synced" ? "已同步" : "仅保存在本机"}</strong></div><div><span>后台整理</span><strong>{processingLabel(entry)}</strong></div><div><span>当前修订</span><strong>第 {entry.revision} 版</strong></div><div><span>内容等级</span><strong>敏感 · 私密</strong></div></div>
      {entry.revisions && entry.revisions.length > 1 && (
        <section className="revision-history">
          <div className="revision-history-heading"><History /><div><strong>修订历史</strong><span>{entry.revisions.length} 个不可变版本</span></div></div>
          {[...entry.revisions].reverse().map((revision) => (
            <article key={revision.id} className={revision.revision === entry.revision ? "current" : ""}>
              <div><strong>第 {revision.revision} 版</strong><span>{fullDateLabel(revision.created_at)}</span>{revision.revision === entry.revision && <em>当前</em>}</div>
              {revision.content ? <p>{revision.content}</p> : <small>该版本正文由后端加密保留；当前接口只返回历史元数据。</small>}
            </article>
          ))}
        </section>
      )}
      <section className="revision-explainer"><History /><div><strong>原始版本不会被静默覆盖</strong><p>修改这条记录会创建新修订。后端会以 ETag 检查并发变化，避免覆盖其他设备上的更新。</p></div></section>
    </div>
  );
}

function InsightsView({ backend, settings, items, selectedId, detail, busy, showSample, onToggleSample, onSelect, onBack, onVerdict, onCorrect, onCreateAction }: {
  backend: BackendState;
  settings: ApiSettings;
  items: MemoryInboxItem[];
  selectedId: string | null;
  detail: MemoryDetail | null;
  busy: string | null;
  showSample: boolean;
  onToggleSample: (value: boolean) => void;
  onSelect: (item: MemoryInboxItem) => void;
  onBack: () => void;
  onVerdict: (item: MemoryInboxItem, verdict: VerdictType) => void;
  onCorrect: (item: MemoryInboxItem) => void;
  onCreateAction: (item?: MemoryInboxItem) => void;
}) {
  const [filter, setFilter] = useState<InsightFilter>("pending");
  const selected = items.find((item) => item.memory_id === selectedId) || (selectedId === SAMPLE_INSIGHT.memory_id ? SAMPLE_INSIGHT : null);
  if (selected) {
    return <MemoryReviewView settings={settings} item={selected} detail={detail} isSample={selected.memory_id === SAMPLE_INSIGHT.memory_id} busy={busy} onBack={onBack} onVerdict={onVerdict} onCorrect={onCorrect} onCreateAction={onCreateAction} />;
  }

  const filtered = items.filter((item) => {
    if (filter === "pending") return !item.current_verdict || item.current_verdict === "snooze";
    if (filter === "confirmed") return ["confirm", "correct"].includes(item.current_verdict || "");
    return ["reject", "retract"].includes(item.current_verdict || "");
  });

  return (
    <div className="content-page insights-page page-enter">
      <PageIntro title="认识" subtitle="系统提出候选，你查看依据并决定它是否贴近自己的感受。" />
      <div className="insight-tabs"><button className={filter === "pending" ? "active" : ""} onClick={() => setFilter("pending")}>待回应</button><button className={filter === "confirmed" ? "active" : ""} onClick={() => setFilter("confirmed")}>已确认</button><button className={filter === "history" ? "active" : ""} onClick={() => setFilter("history")}>历史</button></div>
      {!backend.capabilities.memory_review && (
        <CapabilityNotice title="认识服务尚未接入当前运行实例" description="后端已经定义 Memory Inbox、证据、反证和用户裁定合约，但默认应用还没有注入服务并挂载路由。这里不会用本地规则冒充 AI 认识。" action={<button onClick={() => onToggleSample(!showSample)}>{showSample ? "收起界面示例" : "查看明确标注的界面示例"}</button>} />
      )}
      {showSample && !backend.capabilities.memory_review && (
        <button className="sample-insight-card" onClick={() => onSelect(SAMPLE_INSIGHT)}><span className="sample-badge">界面示例 · 非真实分析</span><h2>{SAMPLE_INSIGHT.version.statement}</h2><p>来自 3 条记录 · 也发现 1 个例外</p><div>查看示例如何区分候选、依据和用户判断 <ArrowRight /></div></button>
      )}
      {backend.capabilities.memory_review && filtered.length > 0 && (
        <div className="insight-list">{filtered.map((item) => <InsightListItem key={item.memory_id} item={item} onClick={() => onSelect(item)} />)}</div>
      )}
      {backend.capabilities.memory_review && !filtered.length && <EmptyState icon={Sparkles} title={filter === "pending" ? "没有等待回应的认识" : "这里暂时是空的"} description={filter === "pending" ? "当系统从多条记录中发现重复线索，会把候选放在这里。" : "你的判断历史会按状态出现在这里。"} />}
    </div>
  );
}

function InsightListItem({ item, onClick }: { item: MemoryInboxItem; onClick: () => void }) {
  const status = item.current_verdict === "confirm" ? "你已确认" : item.current_verdict === "correct" ? "已按你的理解修正" : item.current_verdict === "reject" ? "你认为不符合" : item.current_verdict === "snooze" ? "稍后再看" : "等你判断";
  return <button className="insight-list-item" onClick={onClick}><div className="insight-meta"><span>{kindLabel(item.kind)}</span><em className={`verdict-${item.current_verdict || "pending"}`}>{status}</em></div><h2>{item.version.statement}</h2><p>{item.version.uncertainty || "这是一个需要由你判断的候选解释。"}</p><div className="evidence-count"><span><BookOpenText />{item.support_count} 条支持</span><span><CircleHelp />{item.counterevidence_count} 个例外</span><ArrowRight /></div></button>;
}

function MemoryReviewView({ settings, item, detail, isSample, busy, onBack, onVerdict, onCorrect, onCreateAction }: {
  settings: ApiSettings;
  item: MemoryInboxItem;
  detail: MemoryDetail | null;
  isSample: boolean;
  busy: string | null;
  onBack: () => void;
  onVerdict: (item: MemoryInboxItem, verdict: VerdictType) => void;
  onCorrect: (item: MemoryInboxItem) => void;
  onCreateAction: (item?: MemoryInboxItem) => void;
}) {
  const decided = item.current_verdict === "confirm" || item.current_verdict === "correct";
  return (
    <div className="memory-review page-enter">
      <button className="back-button" onClick={onBack}><ArrowLeft />返回认识</button>
      {isSample && <div className="sample-watermark"><Info />界面示例，不是对你的分析，也不会保存任何操作。</div>}
      <div className="memory-review-grid">
        <article className="memory-statement">
          <div className="memory-kicker"><Sparkles /><span>一个可能的发现</span><em>{item.current_verdict ? "已有你的判断" : "等你判断"}</em></div>
          <h1>{item.version.statement}</h1>
          <p>{item.version.uncertainty || "这些线索支持一种可能解释，但不代表完整的你。"}</p>
          <div className="memory-provenance"><span>{kindLabel(item.kind)}</span><span>{item.version.epistemic_type === "inferred" ? "系统推断" : "用户表述"}</span><span>版本 {item.version.version_no}</span></div>
          {!isSample && busy === "memory-detail" && <div className="inline-loading"><LoaderCircle className="spin" />正在读取权威详情</div>}
          {!item.current_verdict && (
            <div className="verdict-area"><h3>这与你的感受符合吗？</h3><div className="verdict-buttons"><button className="primary-verdict" disabled={isSample || busy === "verdict"} onClick={() => onVerdict(item, "confirm")}><Check />符合我的感受</button><button disabled={isSample || busy === "verdict"} onClick={() => onCorrect(item)}><PenLine />不完全是</button><button disabled={isSample || busy === "verdict"} onClick={() => onVerdict(item, "reject")}><XCircle />这不符合我</button><button disabled={isSample || busy === "verdict"} onClick={() => onVerdict(item, "snooze")}><Clock3 />稍后再看</button></div></div>
          )}
          {item.current_verdict && <div className="decided-banner"><CheckCircle2 /><div><strong>{item.current_verdict === "confirm" ? "这是你当前认可的理解" : item.current_verdict === "correct" ? "已按你的理解修正" : item.current_verdict === "snooze" ? "已暂缓判断" : "已记录为不符合"}</strong><span>用户判断优先于系统候选，并且之后仍可改变。</span></div></div>}
          {decided && <button className="create-action-link" disabled={busy === "create-action"} onClick={() => onCreateAction(item)}>{busy === "create-action" ? <LoaderCircle className="spin" /> : <Footprints />}{busy === "create-action" ? "正在创建小行动" : "创建一个可撤销的小行动"} <ArrowRight /></button>}
        </article>
        <aside className="evidence-panel">
          <div className="evidence-panel-header"><div><span>判断依据</span><small>帮助你判断，不是证明系统正确</small></div><em>{item.support_count + item.counterevidence_count} 条线索</em></div>
          {isSample ? (
            <><EvidenceQuote relation="supports" date="今天 15:42" text="开会时其实有一个不同想法，但我还是先说了「可能是我想多了」。" /><EvidenceQuote relation="supports" date="8月18日" text="发出方案前，我把已经确认过的结论又删掉了一次。" /><EvidenceQuote relation="contradicts" date="一个例外" text="和熟悉的同事讨论时，我通常能直接说出不同意见。" /></>
          ) : detail ? (
            <>
              {detail.evidence.map((anchor) => <EvidenceAnchorRow key={anchor.id} settings={settings} memoryId={item.memory_id} anchor={anchor} />)}
              {detail.counterevidence.map((anchor) => <EvidenceAnchorRow key={anchor.id} settings={settings} memoryId={item.memory_id} anchor={anchor} />)}
              {detail.contextual_evidence.map((anchor) => <EvidenceAnchorRow key={anchor.id} settings={settings} memoryId={item.memory_id} anchor={anchor} />)}
              {!detail.evidence.length && !detail.counterevidence.length && <p className="evidence-empty">当前没有可用的证据锚点。</p>}
              <div className="anchor-disclosure"><Info /><span>原文只在你主动展开时从当前授权的 Source 中读取，不会根据摘要补写或猜测。</span></div>
            </>
          ) : (
            <div className="evidence-skeleton"><span /><span /><span /></div>
          )}
          <div className="source-semantics"><ShieldCheck /><span>{detail?.source_semantics || "记录证明的是你曾这样写下，不自动证明事件的客观真实性。"}</span></div>
        </aside>
      </div>
    </div>
  );
}

function EvidenceQuote({ relation, date, text }: { relation: "supports" | "contradicts"; date: string; text: string }) {
  return <blockquote className={`evidence-quote ${relation}`}><span>{relation === "supports" ? "支持线索" : "也有例外"}</span><p>“{text}”</p><small>{date}</small></blockquote>;
}

function EvidenceAnchorRow({ settings, memoryId, anchor }: { settings: ApiSettings; memoryId: string; anchor: MemoryDetail["evidence"][number] }) {
  const [excerpt, setExcerpt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reveal = async () => {
    setLoading(true);
    setError(null);
    const result = await getEvidenceExcerpt(settings, memoryId, anchor.id);
    if (result.ok) setExcerpt(result.data.excerpt);
    else setError(safeApiMessage(result, "当前无法读取这段原文"));
    setLoading(false);
  };
  return <div className={`evidence-anchor ${anchor.relation}`}><div><span>{anchor.relation === "supports" ? "支持线索" : anchor.relation === "contradicts" ? "反证 / 例外" : "上下文"}</span><em>{anchor.strength_band === "strong" ? "较强" : anchor.strength_band === "moderate" ? "中等" : "较弱"}</em></div>{excerpt ? <blockquote>“{excerpt}”</blockquote> : <p>来源记录 {anchor.source_document_id.slice(0, 8)}… · 修订 {anchor.source_revision_id.slice(0, 8)}…</p>}<small>{anchor.source_recorded_at ? fullDateLabel(anchor.source_recorded_at) : "来源时间未知"}</small>{error && <small className="evidence-error">{error}</small>}{!excerpt && <button className="reveal-evidence" disabled={loading} onClick={() => void reveal()}>{loading ? <LoaderCircle className="spin" /> : <BookOpenText />}{loading ? "正在读取" : "查看真实原文"}</button>}</div>;
}

function ActionsView({ actions, backend, onCreate, onUpdate, busy }: { actions: LocalAction[]; backend: BackendState; onCreate: () => void; onUpdate: (id: string, patch: Partial<LocalAction>) => void; busy: string | null }) {
  const [filter, setFilter] = useState<"current" | "history">("current");
  const filtered = actions.filter((action) => filter === "current" ? ["candidate", "accepted"].includes(action.state) : ["completed", "revoked"].includes(action.state));
  return (
    <div className="content-page actions-page page-enter">
      <PageIntro title="行动" subtitle="一个小行动只有在你接受后才成为承诺；没有逾期、连续失败或羞耻反馈。"><button className="primary-action" onClick={onCreate}><Plus />新建行动候选</button></PageIntro>
      {!backend.capabilities.actions && <div className="local-prototype-note"><Info /><span><strong>当前行动保存在本机</strong>后端 Action 领域已经有候选、任务、实验和授权边界，但 HTTP API 尚未暴露。界面不会把本机状态冒充成服务器状态。</span></div>}
      <div className="insight-tabs"><button className={filter === "current" ? "active" : ""} onClick={() => setFilter("current")}>当前</button><button className={filter === "history" ? "active" : ""} onClick={() => setFilter("history")}>历史</button></div>
      {filtered.length ? <div className="action-list">{filtered.map((action) => <ActionCard key={action.id} action={action} onUpdate={onUpdate} busy={busy === `action:${action.id}`} />)}</div> : <EmptyState icon={Footprints} title={filter === "current" ? "还没有正在尝试的行动" : "还没有行动历史"} description="可以自己写一个，或从一条已经确认的认识开始。" action={filter === "current" ? <button onClick={onCreate}>创建行动候选</button> : undefined} />}
    </div>
  );
}

function ActionCard({ action, onUpdate, busy }: { action: LocalAction; onUpdate: (id: string, patch: Partial<LocalAction>) => void; busy: boolean }) {
  const stateLabel = { candidate: "等你选择", accepted: "你准备尝试", completed: "已记下结果", revoked: "已撤销" }[action.state];
  return <article className={`action-card state-${action.state}`}><div className="action-card-top"><span><Footprints />{stateLabel}</span><small>约 {action.durationMinutes} 分钟</small></div><h2>{action.title}</h2>{action.note && <p>{action.note}</p>}<div className="action-context"><Clock3 />{action.context}</div>{action.sourceStatement && <div className="action-source"><Sparkles /><span>来自你认可的认识：{action.sourceStatement}</span></div>}{action.state === "candidate" && <div className="action-card-buttons"><button className="accept-action" disabled={busy} onClick={() => onUpdate(action.id, { state: "accepted" })}>{busy ? <LoaderCircle className="spin" /> : <Check />}我愿意试试</button><button disabled={busy} onClick={() => onUpdate(action.id, { state: "revoked" })}>现在不需要</button></div>}{action.state === "accepted" && <div className="action-card-buttons"><button className="accept-action" disabled={busy} onClick={() => onUpdate(action.id, { state: "completed", reflection: "unclear" })}>{busy ? <LoaderCircle className="spin" /> : <CheckCircle2 />}记下结果</button><button disabled={busy} onClick={() => onUpdate(action.id, { state: "revoked" })}><Undo2 />撤销</button></div>}{action.state === "completed" && <><div className="action-result"><CheckCircle2 /><span>已记下。不评价成功或失败。</span></div><div className="action-card-buttons"><button disabled={busy} onClick={() => onUpdate(action.id, { state: "revoked" })}><Undo2 />撤销此行动</button></div></>}{action.state === "revoked" && <div className="action-result muted"><Archive /><span>已撤销，不会继续提醒。</span></div>}</article>;
}

function SettingsView({ settings, backend, appVersion, hidePreview, setHidePreview, privacyMask, setPrivacyMask, onSave, onRefresh }: {
  settings: ApiSettings;
  backend: BackendState;
  appVersion: string;
  hidePreview: boolean;
  setHidePreview: (value: boolean) => void;
  privacyMask: boolean;
  setPrivacyMask: (value: boolean) => void;
  onSave: (settings: ApiSettings) => Promise<void>;
  onRefresh: () => void;
}) {
  const [draftSettings, setDraftSettings] = useState(settings);
  const [showToken, setShowToken] = useState(false);
  useEffect(() => setDraftSettings(settings), [settings]);
  return (
    <div className="content-page settings-page page-enter">
      <PageIntro title="设置" subtitle="隐私偏好面向日常使用；后端诊断信息放在更低层级。" />
      <section className="settings-section"><div className="settings-section-heading"><div><ShieldCheck /><span><strong>隐私与显示</strong><small>默认私密，不提供公开分享入口</small></span></div></div><ToggleRow title="锁屏时隐藏正文" description="通知和系统预览不显示记录内容" checked={hidePreview} onChange={setHidePreview} /><ToggleRow title="隐私遮罩模式" description="临时模糊主内容区域，适合身边有人时" checked={privacyMask} onChange={setPrivacyMask} /></section>
      <section className="settings-section"><div className="settings-section-heading"><div><Wifi /><span><strong>后端连接</strong><small>令牌只保留到本次应用关闭</small></span></div><BackendStatusPill backend={backend} /></div><label className="settings-field"><span>API 服务地址</span><input value={draftSettings.baseUrl} onChange={(event) => setDraftSettings({ ...draftSettings, baseUrl: event.target.value })} placeholder="http://127.0.0.1:8000" /></label><label className="settings-field"><span>Vault ID</span><input value={draftSettings.vaultId} onChange={(event) => setDraftSettings({ ...draftSettings, vaultId: event.target.value })} placeholder="00000000-0000-0000-0000-000000000000" spellCheck={false} /></label><label className="settings-field"><span>访问令牌 <small>只用于本地认证，不是模型 API Key</small></span><div className="password-field"><KeyRound /><input autoComplete="off" type={showToken ? "text" : "password"} value={draftSettings.token} onChange={(event) => setDraftSettings({ ...draftSettings, token: event.target.value })} /><button type="button" onClick={() => setShowToken((value) => !value)} aria-label={showToken ? "隐藏访问令牌" : "显示访问令牌"} title={showToken ? "隐藏令牌" : "显示令牌"}>{showToken ? <EyeOff /> : <Eye />}</button></div></label><div className="settings-actions"><button className="primary-action" onClick={() => void onSave(draftSettings)} disabled={backend.phase === "checking"}><RefreshCw className={backend.phase === "checking" ? "spin" : ""} />{backend.phase === "checking" ? "正在检测" : "保存并检测"}</button><button onClick={onRefresh} disabled={backend.phase === "checking"}>重新检查</button></div></section>
      <section className="settings-section"><div className="settings-section-heading"><div><Gauge /><span><strong>能力接入状态</strong><small>仅用于诊断，不影响本机记录</small></span></div><span className="server-version">后端 {backend.serverVersion || "未识别"}</span></div><div className="capability-grid"><Capability name="记录读写" active={backend.capabilities.entries} note="Source API" /><Capability name="记录修订" active={backend.capabilities.entry_revisions} note="ETag / PATCH" /><Capability name="删除与级联" active={backend.capabilities.entry_deletion} note="Tombstone" /><Capability name="候选生成" active={backend.capabilities.candidate_insights} note="Governed Model" /><Capability name="认识审阅" active={backend.capabilities.memory_review} note="Memory Inbox" /><Capability name="用户裁定" active={backend.capabilities.memory_verdicts} note="Verdict" /><Capability name="行动同步" active={backend.capabilities.actions} note="Action API" /><Capability name="模型回执" active={backend.capabilities.model_run_receipts} note="Model Runs" /></div></section>
      <section className="about-row"><span className="brand-mark"><Feather /></span><div><strong>Vistora {appVersion}</strong><p>由你校订、带出处、可撤回的私人生活模型。</p></div><span>Windows x64</span></section>
    </div>
  );
}

function BackendStatusPill({ backend }: { backend: BackendState }) {
  const label = backend.phase === "checking" ? "检查中" : backend.phase === "online" ? (backend.ready ? "已就绪" : "已连接") : "离线";
  return <span className={`backend-status-pill ${backend.phase}`} role="status" aria-label={`后端${label}`}>{backend.phase === "checking" ? <LoaderCircle className="spin" /> : <i aria-hidden="true" />}<span>{label}</span></span>;
}

function Capability({ name, active, note }: { name: string; active: boolean; note: string }) {
  return <div className={`capability ${active ? "available" : "pending"}`}><span>{active ? <Check /> : <Minus />}</span><div><strong>{name}</strong><small>{note}</small></div><em>{active ? "可用" : "待接入"}</em></div>;
}

function ToggleRow({ title, description, checked, onChange }: { title: string; description: string; checked: boolean; onChange: (value: boolean) => void }) {
  return <div className="toggle-row"><span><strong>{title}</strong><small>{description}</small></span><button role="switch" aria-checked={checked} className={checked ? "on" : ""} onClick={() => onChange(!checked)}><i /></button></div>;
}

function PageIntro({ title, subtitle, children }: { title: string; subtitle: string; children?: ReactNode }) {
  return <header className="page-intro"><div><h1>{title}</h1><p>{subtitle}</p></div>{children}</header>;
}

function EmptyState({ icon: Icon, title, description, action }: { icon: typeof Search; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span><Icon /></span><h2>{title}</h2><p>{description}</p>{action}</div>;
}

function CapabilityNotice({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="capability-notice"><div><CircleAlert /><span><strong>{title}</strong><p>{description}</p></span></div>{action}</div>;
}

function ModalFrame({ children, onClose, className = "" }: { children: ReactNode; onClose: () => void; className?: string }) {
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className={`modal-card ${className}`} role="dialog" aria-modal="true">{children}</section></div>;
}

function ComposerModal({ draft, setDraft, busy, onClose, onSave }: { draft: string; setDraft: (value: string) => void; busy: boolean; onClose: () => void; onSave: () => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { window.setTimeout(() => ref.current?.focus(), 80); }, []);
  return <ModalFrame onClose={onClose} className="composer-modal"><header><div><span>新记录</span><h2>此刻有什么值得留下？</h2></div><button onClick={onClose}><X /></button></header><textarea ref={ref} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="可以只写一句话，不需要整理得很完整……" onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") onSave(); }} /><footer><span><LockKeyhole />仅自己可见 · 草稿自动保存</span><div><kbd>Ctrl Enter</kbd><button className="primary-action" disabled={!draft.trim() || busy} onClick={onSave}>{busy ? <LoaderCircle className="spin" /> : <Check />}保存记录</button></div></footer></ModalFrame>;
}

function EditEntryModal({ entry, value, setValue, busy, onClose, onSave }: { entry: Entry; value: string; setValue: (value: string) => void; busy: boolean; onClose: () => void; onSave: () => void }) {
  return <ModalFrame onClose={onClose} className="edit-modal"><header><div><span>创建第 {entry.revision + 1} 版</span><h2>修改不会覆盖原始版本</h2></div><button onClick={onClose}><X /></button></header><div className="original-preview"><span>当前版本</span><p>{entry.content}</p></div><label><span>新版本</span><textarea value={value} onChange={(event) => setValue(event.target.value)} /></label><footer><button onClick={onClose}>取消</button><button className="primary-action" disabled={!value.trim() || value.trim() === entry.content.trim() || busy} onClick={onSave}>{busy ? <LoaderCircle className="spin" /> : <Check />}保存新修订</button></footer></ModalFrame>;
}

function DeleteEntryDialog({ entry, busy, onClose, onConfirm }: { entry: Entry; busy: boolean; onClose: () => void; onConfirm: () => void }) {
  return <ModalFrame onClose={onClose} className="confirm-dialog"><span className="danger-icon"><Trash2 /></span><h2>删除这条记录？</h2><p>删除会先隔离原记录，并让后台重新检查引用它的派生内容。</p><div className="delete-impact"><span><Minus />原始记录将不再显示</span><span><Minus />候选认识可能失效或被撤回</span><span><Minus />已确认认识若失去全部依据，会标为来源已删除</span><span><Clock3 />彻底擦除可能需要一些时间</span></div><div className="dialog-actions"><button onClick={onClose}>保留记录</button><button className="danger-button" disabled={busy} onClick={onConfirm}>{busy ? <LoaderCircle className="spin" /> : <Trash2 />}确认删除</button></div></ModalFrame>;
}

function CorrectionModal({ item, value, setValue, busy, onClose, onSave }: { item: MemoryInboxItem; value: string; setValue: (value: string) => void; busy: boolean; onClose: () => void; onSave: () => void }) {
  return <ModalFrame onClose={onClose} className="correction-modal"><header><div><span>由你重新描述</span><h2>哪里不完全准确？</h2></div><button onClick={onClose}><X /></button></header><div className="candidate-preview"><Sparkles /><p>{item.version.statement}</p></div><label><span>如果由你来描述，更接近什么？</span><textarea value={value} onChange={(event) => setValue(event.target.value)} placeholder="写下更贴近你的版本……" /></label><p className="modal-help"><ShieldCheck />你的文本会成为主版本，系统原版本只保留在历史中。</p><footer><button onClick={onClose}>取消</button><button className="primary-action" disabled={!value.trim() || busy} onClick={onSave}>{busy ? <LoaderCircle className="spin" /> : <Check />}保存我的版本</button></footer></ModalFrame>;
}

function ActionEditor({ value, setValue, onClose, onSave }: { value: { title: string; note: string; durationMinutes: number; context: string; sourceMemoryId: string; sourceStatement: string }; setValue: Dispatch<SetStateAction<{ title: string; note: string; durationMinutes: number; context: string; sourceMemoryId: string; sourceStatement: string }>>; onClose: () => void; onSave: () => void }) {
  return <ModalFrame onClose={onClose} className="action-editor"><header><div><span>行动候选</span><h2>写一个足够小的尝试</h2></div><button onClick={onClose}><X /></button></header>{value.sourceStatement && <div className="candidate-preview"><Sparkles /><p>{value.sourceStatement}</p></div>}<label><span>我想尝试</span><input value={value.title} onChange={(event) => setValue((current) => ({ ...current, title: event.target.value }))} placeholder="例如：会议前先写下一句真正想表达的话" /></label><div className="action-editor-grid"><label><span>预计用时</span><select value={value.durationMinutes} onChange={(event) => setValue((current) => ({ ...current, durationMinutes: Number(event.target.value) }))}><option value={3}>约 3 分钟</option><option value={5}>约 5 分钟</option><option value={10}>约 10 分钟</option><option value={20}>约 20 分钟</option></select></label><label><span>适用情境</span><input value={value.context} onChange={(event) => setValue((current) => ({ ...current, context: event.target.value }))} /></label></div><label><span>给自己的说明 <small>可选</small></span><textarea value={value.note} onChange={(event) => setValue((current) => ({ ...current, note: event.target.value }))} /></label><footer><button onClick={onClose}>现在不需要</button><button className="primary-action" disabled={!value.title.trim()} onClick={onSave}><Check />保存为候选</button></footer></ModalFrame>;
}

function SearchPalette({ query, setQuery, results, onClose, onEntry, onMemory, onAction }: { query: string; setQuery: (value: string) => void; results: { entries: Entry[]; memories: MemoryInboxItem[]; actions: LocalAction[] }; onClose: () => void; onEntry: (entry: Entry) => void; onMemory: (memory: MemoryInboxItem) => void; onAction: (action: LocalAction) => void }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { window.setTimeout(() => ref.current?.focus(), 70); }, []);
  const hasResults = results.entries.length + results.memories.length + results.actions.length > 0;
  return <ModalFrame onClose={onClose} className="search-palette"><label className="palette-input"><Search /><input ref={ref} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索原话、认识和行动" /><kbd>Esc</kbd></label><div className="search-results">{!query.trim() && <div className="search-hint"><Command /><p>输入关键词，Vistora 只在这台设备和已经加载的内容中搜索。</p></div>}{query.trim() && !hasResults && <EmptyState icon={Search} title="没有找到" description="试试记录中的另一段原话。" />}{results.entries.length > 0 && <SearchGroup title="记录">{results.entries.map((entry) => <button key={entry.id} onClick={() => onEntry(entry)}><NotebookPen /><span><strong>{entry.content}</strong><small>{relativeDayLabel(entry.captured_at)}</small></span><ChevronRight /></button>)}</SearchGroup>}{results.memories.length > 0 && <SearchGroup title="认识">{results.memories.map((memory) => <button key={memory.memory_id} onClick={() => onMemory(memory)}><Sparkles /><span><strong>{memory.version.statement}</strong><small>{kindLabel(memory.kind)}</small></span><ChevronRight /></button>)}</SearchGroup>}{results.actions.length > 0 && <SearchGroup title="行动">{results.actions.map((action) => <button key={action.id} onClick={() => onAction(action)}><Footprints /><span><strong>{action.title}</strong><small>{action.state}</small></span><ChevronRight /></button>)}</SearchGroup>}</div></ModalFrame>;
}

function SearchGroup({ title, children }: { title: string; children: ReactNode }) {
  return <section className="search-group"><h3>{title}</h3>{children}</section>;
}

export default App;
