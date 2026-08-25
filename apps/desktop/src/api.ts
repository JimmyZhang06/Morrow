import type {
  ApiSettings,
  ActionResource,
  ActionVerdictResponse,
  ActionVerdictType,
  BackendCapabilities,
  CandidateInsightGeneration,
  Entry,
  EvidenceExcerpt,
  MemoryDetail,
  MemoryInboxItem,
  VerdictType,
} from "./types";

export type ApiResult<T> = DesktopApiResponse<T>;

async function browserRequest<T>(input: DesktopApiRequest): Promise<ApiResult<T>> {
  const controller = new AbortController();
  const timeoutMs = Math.min(Math.max(input.timeoutMs ?? 8_000, 1_000), 60_000);
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const normalizedBase = input.baseUrl.replace(/\/$/, "");
    const isLocalDevTarget =
      import.meta.env.DEV &&
      ["127.0.0.1", "localhost"].includes(window.location.hostname) &&
      /^http:\/\/(?:127\.0\.0\.1|localhost):\d+$/.test(normalizedBase);
    const requestUrl = isLocalDevTarget
      ? `/__vistora_api${input.path}`
      : `${normalizedBase}${input.path}`;
    const response = await fetch(requestUrl, {
      method: input.method || "GET",
      headers: {
        Accept: "application/json",
        ...(input.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(input.token ? { Authorization: `Bearer ${input.token}` } : {}),
        ...(input.vaultId ? { "X-Vault-ID": input.vaultId } : {}),
        ...(input.headers || {}),
      },
      body: input.body === undefined ? undefined : JSON.stringify(input.body),
      signal: controller.signal,
    });
    const raw = await response.text();
    let data: T;
    try {
      data = (raw ? JSON.parse(raw) : null) as T;
    } catch {
      data = { message: raw.slice(0, 800) } as T;
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
      headers: {
        etag: response.headers.get("etag"),
        contentType: response.headers.get("content-type"),
      },
    };
  } catch {
    return {
      ok: false,
      status: 0,
      data: { message: "无法连接后端服务" } as T,
      headers: {},
    };
  } finally {
    window.clearTimeout(timer);
  }
}

async function request<T>(
  settings: ApiSettings,
  input: Omit<DesktopApiRequest, "baseUrl" | "token" | "vaultId">,
) {
  const payload = {
    ...input,
    baseUrl: settings.baseUrl,
    token: settings.token.trim() || undefined,
    vaultId: settings.vaultId.trim() || undefined,
  };
  // Keep development requests on the same-origin Vite proxy. This makes the
  // desktop dev runtime use the configured local API port consistently while
  // the packaged app continues to use Electron's isolated IPC bridge.
  if (import.meta.env.DEV) return browserRequest<T>(payload);
  if (window.vistoraDesktop) return window.vistoraDesktop.apiRequest<T>(payload);
  return browserRequest<T>(payload);
}

export function safeApiMessage(result: ApiResult<unknown>, fallback: string) {
  const data = result.data as {
    message?: string;
    safe_detail?: string;
    detail?: string | { safe_detail?: string };
  } | null;
  if (!data) return fallback;
  if (typeof data.detail === "object" && data.detail?.safe_detail) return data.detail.safe_detail;
  if (typeof data.detail === "string") return data.detail;
  return data.safe_detail || data.message || fallback;
}

export const checkHealth = (settings: ApiSettings) =>
  request<{ status: "live" }>(settings, { path: "/health/live" });

export const checkReadiness = (settings: ApiSettings) =>
  request<{ status: "ready" }>(settings, { path: "/health/ready" });

export const getCapabilities = (settings: ApiSettings) =>
  request<{ api_version: "v1"; server_version: string; features: BackendCapabilities }>(
    settings,
    { path: "/health/capabilities" },
  );

export const listEntries = (settings: ApiSettings) =>
  request<{ items: Entry[]; next_cursor: string | null }>(settings, {
    path: "/v1/entries?limit=100",
  });

export const getEntry = (settings: ApiSettings, entryId: string) =>
  request<Entry>(settings, { path: `/v1/entries/${encodeURIComponent(entryId)}` });

export function createEntry(
  settings: ApiSettings,
  content: string,
  clientId: string,
  capturedAt = new Date().toISOString(),
) {
  return request<{
    id: string;
    revision: number;
    saved: true;
    processing: { state: Entry["processing"]["state"] };
  }>(settings, {
    path: "/v1/entries",
    method: "POST",
    headers: { "Idempotency-Key": clientId },
    body: {
      content,
      captured_at: capturedAt,
      memory_policy: "default",
      client_id: clientId,
      source_type: "note",
      data_class: "sensitive",
    },
  });
}

export function appendEntryRevision(
  settings: ApiSettings,
  entryId: string,
  content: string,
  revision: number,
) {
  return request<{
    id: string;
    revision: number;
    saved: true;
    processing: { state: Entry["processing"]["state"] };
  }>(settings, {
    path: `/v1/entries/${encodeURIComponent(entryId)}`,
    method: "PATCH",
    headers: { "If-Match": `"${revision}"`, "Idempotency-Key": crypto.randomUUID() },
    body: { content, expected_revision: revision },
  });
}

export function deleteEntry(settings: ApiSettings, entryId: string, revision: number) {
  return request<{
    id: string;
    tombstoned: true;
    source_generation: number;
    policy_epoch: number;
    cascade_state: "planned";
  }>(settings, {
    path: `/v1/entries/${encodeURIComponent(entryId)}`,
    method: "DELETE",
    headers: { "If-Match": `"${revision}"`, "Idempotency-Key": crypto.randomUUID() },
  });
}

export const listMemoryInbox = (settings: ApiSettings) =>
  request<{ items: MemoryInboxItem[]; next_cursor: string | null }>(settings, {
    path: "/v1/memory-inbox?limit=100",
  });

export const listMemories = (settings: ApiSettings) =>
  request<{ items: MemoryInboxItem[]; next_cursor: string | null }>(settings, {
    path: "/v1/memories?limit=100",
  });

export const getMemoryDetail = (settings: ApiSettings, memoryId: string) =>
  request<MemoryDetail>(settings, { path: `/v1/memories/${encodeURIComponent(memoryId)}` });

export function submitMemoryVerdict(
  settings: ApiSettings,
  memoryId: string,
  etag: string,
  verdict: VerdictType,
  correction?: string,
) {
  return request<{
    verdict_id: string;
    memory_id: string;
    current_derived_object_id: string;
    state: string;
    version_no: number;
    etag: string;
  }>(settings, {
    path: `/v1/memories/${encodeURIComponent(memoryId)}/verdicts`,
    method: "POST",
    headers: { "If-Match": etag },
    body: {
      verdict,
      ...(verdict === "correct"
        ? {
            replacement: {
              statement: correction?.trim(),
              mode: "interpretation_error",
              confidence_band: "medium",
            },
          }
        : {}),
    },
  });
}

export function generateCandidateInsight(
  settings: ApiSettings,
  entryId: string,
  revision: number,
  idempotencyKey: string = crypto.randomUUID(),
) {
  return request<CandidateInsightGeneration>(settings, {
    path: `/v1/entries/${encodeURIComponent(entryId)}/candidate-insights`,
    method: "POST",
    timeoutMs: 10_000,
    headers: {
      "If-Match": `"${revision}"`,
      "Idempotency-Key": idempotencyKey,
    },
    body: {},
  });
}

export function getCandidateInsightJob(settings: ApiSettings, jobId: string) {
  return request<CandidateInsightGeneration>(settings, {
    path: `/v1/candidate-insight-jobs/${encodeURIComponent(jobId)}`,
  });
}

export function cancelCandidateInsightJob(settings: ApiSettings, jobId: string) {
  return request<CandidateInsightGeneration>(settings, {
    path: `/v1/candidate-insight-jobs/${encodeURIComponent(jobId)}`,
    method: "DELETE",
  });
}

export function getEvidenceExcerpt(
  settings: ApiSettings,
  memoryId: string,
  evidenceId: string,
) {
  return request<EvidenceExcerpt>(settings, {
    path: `/v1/memories/${encodeURIComponent(memoryId)}/evidence/${encodeURIComponent(evidenceId)}/excerpt`,
  });
}

export function createAction(
  settings: ApiSettings,
  memoryId: string,
  idempotencyKey: string = crypto.randomUUID(),
) {
  return request<ActionResource>(settings, {
    path: `/v1/memories/${encodeURIComponent(memoryId)}/actions`,
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: {},
  });
}

export function getAction(settings: ApiSettings, actionId: string) {
  return request<ActionResource>(settings, {
    path: `/v1/actions/${encodeURIComponent(actionId)}`,
  });
}

export const listActions = (settings: ApiSettings) =>
  request<{ items: ActionResource[]; next_cursor: string | null }>(settings, {
    path: "/v1/actions?limit=100",
  });

export function submitActionVerdict(
  settings: ApiSettings,
  actionId: string,
  verdict: ActionVerdictType,
  etag?: string,
  idempotencyKey: string = crypto.randomUUID(),
) {
  return request<ActionVerdictResponse>(settings, {
    path: `/v1/actions/${encodeURIComponent(actionId)}/verdicts`,
    method: "POST",
    headers: {
      "Idempotency-Key": idempotencyKey,
      ...(etag ? { "If-Match": etag } : {}),
    },
    body: { verdict },
  });
}
