export type ProcessingState = "ready" | "pending" | "partial" | "failed" | "delayed";
export type SyncState = "synced" | "local" | "syncing" | "failed" | "demo";

export type Entry = {
  id: string;
  title?: string | null;
  content: string;
  captured_at: string;
  created_at: string;
  revision: number;
  revision_id?: string;
  capture_timezone?: string;
  source_type: "note" | "conversation" | "audio" | "image" | "file" | "import" | "correction";
  data_class: "normal" | "sensitive" | "highly_sensitive";
  processing: { state: ProcessingState };
  revisions?: Array<{
    id: string;
    revision: number;
    created_at: string;
    content_mime: string;
    language?: string | null;
    content?: string;
  }>;
  syncState?: SyncState;
};

export type ApiSettings = { baseUrl: string; token: string; vaultId: string };

export type BackendCapabilities = {
  entries: boolean;
  entry_revisions: boolean;
  entry_deletion: boolean;
  memory_review: boolean;
  memory_verdicts: boolean;
  candidate_insights: boolean;
  actions: boolean;
  narratives: boolean;
  calendar_candidates: boolean;
  model_run_receipts: boolean;
};

export type BackendState = {
  phase: "checking" | "online" | "offline";
  ready: boolean | null;
  serverVersion: string | null;
  capabilities: BackendCapabilities;
  lastCheckedAt: string | null;
  message: string | null;
};

export type MemoryClaimKind =
  | "explicit_fact"
  | "preference"
  | "value"
  | "goal"
  | "relationship"
  | "self_description"
  | "pattern_hypothesis";

export type VerdictType = "confirm" | "correct" | "reject" | "snooze" | "retract";

export type ClaimVersion = {
  derived_object_id: string;
  version_no: number;
  statement: string;
  structured_payload: Record<string, unknown>;
  epistemic_type: "stated" | "observed" | "inferred" | "user_authored";
  attribution: "self_report" | "quoted_other" | "imported_record" | "model_hypothesis";
  uncertainty?: string | null;
  state: "candidate" | "active" | "disputed" | "superseded" | "retracted";
  confidence_band: "low" | "medium" | "high";
  origin: "pipeline_derived" | "user_correction";
  correction_mode?: "interpretation_error" | "life_stage_change" | null;
  valid_time: {
    from: string;
    to?: string | null;
    precision: string;
    original_expression?: string | null;
    timezone?: string | null;
  };
  system_time: { from: string; to?: string | null };
};

export type EvidenceAnchor = {
  id: string;
  source_document_id: string;
  source_revision_id: string;
  source_fragment_id: string;
  relation: "supports" | "contradicts" | "contextualizes";
  quote_start?: number | null;
  quote_end?: number | null;
  quote_hash: string;
  extractor_reason: string;
  strength_band: "weak" | "moderate" | "strong";
  source_recorded_at?: string | null;
  source_data_class: "normal" | "sensitive" | "highly_sensitive";
};

export type MemoryInboxItem = {
  memory_id: string;
  kind: MemoryClaimKind;
  version: ClaimVersion;
  support_count: number;
  counterevidence_count: number;
  current_verdict?: VerdictType | null;
  etag: string;
};

export type MemoryDetail = {
  memory_id: string;
  kind: MemoryClaimKind;
  version: ClaimVersion;
  history: ClaimVersion[];
  evidence: EvidenceAnchor[];
  counterevidence: EvidenceAnchor[];
  contextual_evidence: EvidenceAnchor[];
  verdicts: Array<{
    id: string;
    verdict: VerdictType;
    correction_text?: string | null;
    reason?: string | null;
    created_at: string;
  }>;
  current_verdict?: VerdictType | null;
  governance_verdict?: VerdictType | null;
  data_class: "normal" | "sensitive" | "highly_sensitive";
  is_current: boolean;
  etag?: string | null;
  allowed_uses: string[];
  source_semantics: string;
};

export type LocalAction = {
  id: string;
  title: string;
  note: string;
  durationMinutes: number;
  context: string;
  sourceMemoryId?: string;
  sourceStatement?: string;
  state: "candidate" | "accepted" | "completed" | "revoked";
  createdAt: string;
  updatedAt: string;
  reflection?: "different" | "unclear" | "not_done" | "not_suitable";
  etag?: string;
  remote?: boolean;
  modelRunId?: string;
};

export type CandidateInsightGeneration = {
  job_id?: string;
  status: "queued" | "processing" | "succeeded" | "failed" | "unknown" | "denied" | "canceling" | "canceled";
  stage?: string;
  progress?: number;
  retryable?: boolean;
  run_id?: string | null;
  memory_id?: string | null;
  derived_object_id?: string | null;
};

export type EvidenceExcerpt = {
  memory_id: string;
  evidence_id: string;
  relation: EvidenceAnchor["relation"];
  source_document_id: string;
  source_revision_id: string;
  source_fragment_id: string;
  source_recorded_at: string;
  source_data_class: EvidenceAnchor["source_data_class"];
  excerpt: string;
  source_semantics: string;
};

export type ActionVerdictType = "accept" | "complete" | "revoke";

export type ActionResource = {
  id?: string;
  action_id?: string;
  memory_id?: string;
  source_memory_id?: string;
  title: string;
  description?: string | null;
  rationale?: string | null;
  exit_plan?: string | null;
  estimated_minutes?: number;
  source_statement?: string | null;
  state: "proposed" | "accepted" | "completed" | "revoked";
  revision: number;
  kind: "reversible_experiment";
  is_reversible: true;
  source_derived_object_id?: string;
  model_run_id?: string | null;
  template_version?: string;
  created_at?: string;
  updated_at?: string;
  etag?: string;
};

export type ActionVerdictResponse = {
  verdict_id: string;
  action_id: string;
  state: "accepted" | "completed" | "revoked";
  revision: number;
  updated_at: string;
};

export type NarrativeProject = {
  project_id: string;
  title: string;
  scope_from?: string | null;
  scope_to?: string | null;
  state: string;
};

export type NarrativeCitation = {
  memory_id: string;
  derived_object_id: string;
  material_ordinal: number;
  relation: "supports" | "counterexample";
};

export type NarrativeTheme = {
  theme_id: string;
  position: number;
  title: string;
  interpretation: string;
  counterpoint: string;
  uncovered_period: string;
  citations: NarrativeCitation[];
};

export type NarrativeGeneration = {
  generation_id: string;
  project_id: string;
  model_run_id: string;
  kind: "life_line" | "memoir_chapter";
  title: string;
  body: string;
  uncertainty: string;
  state: string;
  created_at: string;
  themes: NarrativeTheme[];
  citations: NarrativeCitation[];
};

export type CalendarCandidate = {
  candidate_id: string;
  generation_id: string;
  title: string;
  starts_at: string;
  ends_at: string;
  timezone: string;
  notes: string;
  state: "proposed" | "confirmed" | "revoked";
  revision: number;
  created_at: string;
  updated_at: string;
};
