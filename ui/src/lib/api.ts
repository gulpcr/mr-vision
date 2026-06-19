const API_BASE = process.env.NEXT_PUBLIC_API_URL || "/api";

async function fetchBlob(path: string): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("auth_token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(`${API_BASE}${path}`, { headers });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.blob();
}

async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string>),
  };

  // Add auth token if available
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("auth_token");
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
  }

  const res = await fetch(`${API_BASE}${path}`, {
    headers,
    ...options,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = error.detail;
    let message: string;
    if (!detail) {
      message = `API error: ${res.status}`;
    } else if (typeof detail === "string") {
      message = detail;
    } else if (Array.isArray(detail)) {
      message = detail.map((d: any) => d.msg || JSON.stringify(d)).join("; ");
    } else {
      message = JSON.stringify(detail);
    }
    throw new Error(message);
  }
  return res.json();
}

export interface Study {
  study_instance_uid: string;
  patient_id: string | null;
  patient_name: string | null;
  patient_sex: string | null;
  patient_age: string | null;
  patient_weight_kg: number | null;
  patient_height_cm: number | null;
  study_date: string | null;
  study_description: string | null;
  accession_number: string | null;
  referring_physician: string | null;
  body_part_examined: string | null;
  modality: string | null;
  institution_name: string | null;
  series: Series[];
  created_at: string;
  updated_at: string;
}

export interface Series {
  series_instance_uid: string;
  series_number: number | null;
  series_description: string | null;
  modality: string | null;
  body_part_examined: string | null;
  protocol_name: string | null;
  slice_thickness: number | null;
  num_instances: number;
}

export interface Job {
  id: string;
  study_instance_uid: string;
  usecase_name: string;
  status: string;
  progress: number;
  status_message: string;
  started_at: string | null;
  completed_at: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

export interface Result {
  id: string;
  job_id: string;
  study_instance_uid: string;
  usecase_name: string;
  summary: Record<string, any>;
  measurements: Record<string, any>;
  qa_flags: string[];
  qa_details: Record<string, any>;
  model_version: string;
  model_checksum: string;
  artifacts: Artifact[];
  version: number;
  is_latest: boolean;
  created_at: string;
}

export interface MeasurementDelta {
  a: number;
  b: number;
  change: number;
  change_pct: number;
  severity: "low" | "medium" | "high";
}

export interface ComparisonData {
  usecase_name: string;
  result_a: Result;
  result_b: Result;
  delta: {
    measurements: Record<string, MeasurementDelta>;
    qa_flags_new: string[];
    qa_flags_resolved: string[];
    days_between: number | null;
  };
}

export interface OrthancStudy {
  orthanc_id: string;
  study_instance_uid: string;
  patient_id: string;
  patient_name: string;
  study_date: string;
  study_description: string;
  modality: string;
  series_count: number;
}

export interface Artifact {
  name: string;
  artifact_type: string;
  storage_path: string;
  content_type: string;
  size_bytes: number;
}

export interface UseCase {
  name: string;
  version: string;
  description: string;
  supported_body_parts: string[];
  required_sequences: string[];
  model_type: string;
  enabled: boolean;
}

export interface AuditEntry {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  actor: string;
  details: Record<string, any>;
  timestamp: string;
}

export interface CptSuggestion {
  code: string;
  description: string;
  confidence: number;
  category: "primary" | "addon";
}

export interface ProtocolIssue {
  series_uid: string;
  series_description: string;
  severity: "warning" | "error" | "info";
  code: string;
  message: string;
  suggestion: string;
}

export interface ProtocolCheckResult {
  study_instance_uid: string;
  series_checked: number;
  issues: ProtocolIssue[];
  status: "ok" | "warnings" | "errors" | "no_series";
}

export interface ShareLink {
  id: string;
  result_id: string;
  study_instance_uid: string;
  usecase_name: string;
  token: string;
  created_by: string;
  expires_at: string;
  is_active: boolean;
  created_at: string;
}

export interface TrendTimepoint {
  study_instance_uid: string;
  study_date: string | null;
  result_id: string;
  measurements: Record<string, any>;
  rano_classification: string | null;
  created_at: string;
}

export interface TrendData {
  patient_id: string;
  usecase_name: string;
  timepoints: TrendTimepoint[];
}

export interface QaMetrics {
  tat_median_minutes: number;
  tat_p75_minutes: number;
  tat_p95_minutes: number;
  tat_by_usecase: Record<string, number>;
  review_queue_stats: Record<string, number>;
  correction_rate_pct: number;
  qa_flag_rate_pct: number;
  jobs_completed: number;
  jobs_failed: number;
}

export interface CapacityMetrics {
  daily_volume: Array<{ date: string; total: number; by_usecase: Record<string, number> }>;
  hourly_heatmap: number[];
  peak_hour: number;
  avg_duration_by_usecase: Record<string, number>;
  forecast_7day: number[];
  last_7days_actual: number[];
}

export interface UrgencyScore {
  study_instance_uid: string;
  score: number;
  priority: "STAT" | "HIGH" | "NORMAL" | "ROUTINE";
  factors: Record<string, number>;
}

export interface ReviewItem {
  id: string;
  study_instance_uid: string;
  usecase_name: string;
  result_id: string;
  confidence_score: number;
  status: string;
  reviewer: string | null;
  review_notes: string;
  reviewed_at: string | null;
  created_at: string;
}

export interface AlertRule {
  id: string;
  name: string;
  event_type: string;
  condition: Record<string, any>;
  webhook_url: string;
  is_active: boolean;
  created_at: string;
}

export interface CriticalAlert {
  id: string;
  study_instance_uid: string;
  usecase_name: string;
  result_id: string;
  patient_id: string | null;
  finding_type: string;
  severity: "CRITICAL" | "WARNING";
  title: string;
  message: string;
  details: Record<string, any>;
  status: "pending" | "acknowledged" | "escalated" | "resolved";
  notification_channels: string[];
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  escalated_at: string | null;
  escalation_count: number;
  created_at: string;
}

export interface CriticalAlertStats {
  pending_critical: number;
  pending_warning: number;
  total_unacknowledged: number;
  total_acknowledged: number;
  total_escalated: number;
}

export interface BatchUpload {
  id: string;
  name: string;
  total_items: number;
  completed_items: number;
  failed_items: number;
  status: string;
  created_by: string;
  created_at: string;
}

export interface Experiment {
  id: string;
  name: string;
  usecase_name: string;
  control_version: string;
  treatment_version: string;
  traffic_split: number;
  is_active: boolean;
  created_at: string;
}

export interface RetentionPolicy {
  id: string;
  name: string;
  entity_type: string;
  max_age_days: number;
  action: string;
  is_active: boolean;
  created_at: string;
}

export const api = {
  studies: {
    list: (params?: Record<string, string>) => {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return fetchAPI<{ studies: Study[]; total: number }>(`/studies${qs}`);
    },
    get: (uid: string) => fetchAPI<Study>(`/studies/${uid}`),
    ingest: (uid: string) =>
      fetchAPI<Study>("/studies", {
        method: "POST",
        body: JSON.stringify({ study_instance_uid: uid }),
      }),
    delete: (uid: string) =>
      fetchAPI<void>(`/studies/${uid}`, { method: "DELETE" }),
  },
  jobs: {
    create: (studyUid: string, usecases?: string[]) =>
      fetchAPI<{ jobs: Job[] }>(`/studies/${studyUid}/jobs`, {
        method: "POST",
        body: JSON.stringify({ usecase_names: usecases || null }),
      }),
    get: (jobId: string) => fetchAPI<Job>(`/jobs/${jobId}`),
    listByStudy: (studyUid: string) =>
      fetchAPI<{ jobs: Job[] }>(`/studies/${studyUid}/jobs`),
    cancel: (jobId: string) =>
      fetchAPI<Job>(`/jobs/${jobId}/cancel`, { method: "POST" }),
    retry: (jobId: string) =>
      fetchAPI<Job>(`/jobs/${jobId}/retry`, { method: "POST" }),
  },
  results: {
    get: (studyUid: string, usecase: string, version?: number) => {
      const qs = version !== undefined ? `?version=${version}` : "";
      return fetchAPI<Result>(`/results/${studyUid}/${usecase}${qs}`);
    },
    listByStudy: (studyUid: string) =>
      fetchAPI<{ results: Result[] }>(`/results/${studyUid}`),
    listVersions: (studyUid: string, usecase: string) =>
      fetchAPI<{ results: Result[] }>(`/results/${studyUid}/${usecase}/versions`),
    compare: (resultIdA: string, resultIdB: string) =>
      fetchAPI<ComparisonData>("/compare", {
        method: "POST",
        body: JSON.stringify({ result_ids: [resultIdA, resultIdB] }),
      }),
  },
  usecases: {
    list: () => fetchAPI<{ usecases: UseCase[] }>("/usecases"),
    getUiSchema: (name: string) => fetchAPI<any>(`/usecases/${name}/ui-schema`),
  },
  admin: {
    getRoutingRules: () =>
      fetchAPI<{ routing_rules: Record<string, any[]> }>("/admin/routing-rules"),
    updateRoutingRules: (rules: any[]) =>
      fetchAPI("/admin/routing-rules", {
        method: "PUT",
        body: JSON.stringify({ rules }),
      }),
    getSiteConfig: () => fetchAPI<any>("/admin/site-config"),
    updateSiteConfig: (config: any) =>
      fetchAPI("/admin/site-config", {
        method: "PUT",
        body: JSON.stringify(config),
      }),
    resetAllData: () =>
      fetchAPI<{ status: string; cleared: Record<string, number> }>(
        "/admin/reset?confirm=true",
        { method: "POST" }
      ),
  },
  orthanc: {
    listStudies: () => fetchAPI<OrthancStudy[]>("/orthanc/studies"),
  },
  auth: {
    login: (username: string, password: string) =>
      fetchAPI<{ access_token: string; token_type: string; user_id: string; username: string; role: string; tenant_id: string }>(
        "/auth/login",
        { method: "POST", body: JSON.stringify({ username, password }) }
      ),
    register: (data: { username: string; email: string; password: string; full_name?: string }) =>
      fetchAPI<any>("/auth/register", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    listUsers: () => fetchAPI<any[]>("/auth/users"),
  },
  audit: {
    list: (params?: Record<string, string>) => {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return fetchAPI<{ entries: AuditEntry[]; total: number }>(`/admin/audit${qs}`);
    },
  },
  review: {
    list: (params?: Record<string, string>) => {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return fetchAPI<{ items: ReviewItem[]; stats: Record<string, number> }>(`/admin/review${qs}`);
    },
    get: (id: string) => fetchAPI<ReviewItem>(`/admin/review/${id}`),
    submit: (id: string, data: { status: string; notes?: string }) =>
      fetchAPI<ReviewItem>(`/admin/review/${id}/submit`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
  },
  alerts: {
    list: () => fetchAPI<{ rules: AlertRule[] }>("/admin/alerts"),
    create: (data: { name: string; event_type: string; webhook_url: string; condition?: any }) =>
      fetchAPI<AlertRule>("/admin/alerts", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    delete: (id: string) => fetchAPI("/admin/alerts/" + id, { method: "DELETE" }),
    history: (params?: Record<string, string>) => {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return fetchAPI<{ history: any[] }>(`/admin/alerts/history${qs}`);
    },
  },
  criticalAlerts: {
    list: (params?: Record<string, string>) => {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return fetchAPI<{ alerts: CriticalAlert[]; count: number }>(`/critical-alerts${qs}`);
    },
    get: (id: string) => fetchAPI<CriticalAlert>(`/critical-alerts/${id}`),
    stats: () => fetchAPI<CriticalAlertStats>("/critical-alerts/stats"),
    acknowledge: (id: string, acknowledgedBy: string) =>
      fetchAPI<CriticalAlert>(`/critical-alerts/${id}/acknowledge`, {
        method: "POST",
        body: JSON.stringify({ acknowledged_by: acknowledgedBy }),
      }),
  },
  experiments: {
    list: () => fetchAPI<{ experiments: Experiment[] }>("/admin/experiments"),
    create: (data: any) =>
      fetchAPI<Experiment>("/admin/experiments", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    stats: (id: string) => fetchAPI<any>(`/admin/experiments/${id}/stats`),
    stop: (id: string) => fetchAPI(`/admin/experiments/${id}/stop`, { method: "POST" }),
  },
  retention: {
    list: () => fetchAPI<{ policies: RetentionPolicy[] }>("/admin/retention"),
    create: (data: any) =>
      fetchAPI<RetentionPolicy>("/admin/retention", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    delete: (id: string) => fetchAPI("/admin/retention/" + id, { method: "DELETE" }),
    apply: () => fetchAPI("/admin/retention/apply", { method: "POST" }),
  },
  batches: {
    list: () => fetchAPI<{ batches: BatchUpload[] }>("/admin/batches"),
    get: (id: string) => fetchAPI<any>(`/admin/batches/${id}`),
    create: (data: { name: string; study_uids: string[] }) =>
      fetchAPI<BatchUpload>("/admin/batches", {
        method: "POST",
        body: JSON.stringify(data),
      }),
  },
  models: {
    listVersions: (usecase: string) =>
      fetchAPI<{ versions: any[] }>(`/admin/models/${usecase}/versions`),
    registerVersion: (usecase: string, data: any) =>
      fetchAPI<any>(`/admin/models/${usecase}/versions`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    activate: (usecase: string, version: string) =>
      fetchAPI(`/admin/models/${usecase}/versions/${version}/activate`, {
        method: "POST",
      }),
    getActive: (usecase: string) =>
      fetchAPI<any>(`/admin/models/${usecase}/active`),
  },
  reports: {
    getPdfUrl: (studyUid: string, usecase: string) =>
      `${API_BASE}/reports/${studyUid}/${usecase}/pdf`,
    getSrUrl: (studyUid: string, usecase: string) =>
      `${API_BASE}/reports/${studyUid}/${usecase}/dicom-sr`,
    getFhirUrl: (studyUid: string, usecase: string) =>
      `${API_BASE}/reports/${studyUid}/${usecase}/fhir`,
  },
  cpt: {
    getSuggestions: (studyUid: string, usecase: string) =>
      fetchAPI<{ result_id: string; usecase_name: string; suggestions: CptSuggestion[] }>(
        `/results/${studyUid}/${usecase}/cpt-suggestions`
      ),
  },
  pdf: {
    downloadBlob: (resultId: string) => fetchBlob(`/results/${resultId}/report.pdf`),
    getUrl: (resultId: string) => `${API_BASE}/results/${resultId}/report.pdf`,
  },
  portal: {
    createShareLink: (resultId: string, createdBy: string, ttlDays: number) =>
      fetchAPI<ShareLink>(`/results/${resultId}/share`, {
        method: "POST",
        body: JSON.stringify({ created_by: createdBy, ttl_days: ttlDays }),
      }),
    getByToken: (token: string) =>
      fetchAPI<{
        portal: boolean;
        expires_at: string;
        result: Result;
        study: {
          patient_name: string | null;
          patient_id: string | null;
          study_date: string | null;
          study_description: string | null;
          institution_name: string | null;
        };
      }>(`/portal/${token}`),
  },
  trend: {
    getPatient: (patientId: string, usecase: string) =>
      fetchAPI<TrendData>(`/admin/patients/${encodeURIComponent(patientId)}/trend/${usecase}`),
  },
  metrics: {
    getQa: (days?: number, usecase?: string) => {
      const params = new URLSearchParams();
      if (days) params.set("days", String(days));
      if (usecase) params.set("usecase_name", usecase);
      return fetchAPI<QaMetrics>(`/admin/metrics?${params}`);
    },
    getCapacity: (days?: number) => {
      const params = days ? `?days=${days}` : "";
      return fetchAPI<CapacityMetrics>(`/admin/capacity${params}`);
    },
    getUrgencyScores: (studyUids: string[]) =>
      fetchAPI<{ scores: UrgencyScore[] }>("/admin/urgency-scores", {
        method: "POST",
        body: JSON.stringify({ study_uids: studyUids }),
      }),
  },
  protocol: {
    check: (studyUid: string, usecase: string) =>
      fetchAPI<ProtocolCheckResult>(`/admin/studies/${studyUid}/protocol-check?usecase_name=${usecase}`),
  },
  priorComparison: {
    get: (studyUid: string, usecase: string) =>
      fetchAPI<ComparisonData>(`/admin/studies/${studyUid}/prior-comparison/${usecase}`),
  },
  fused: {
    // Slice counts + default (max-uptake) slice per view for the interactive viewer.
    meta: (studyUid: string, usecase: string) =>
      fetchAPI<FusedMeta>(`/fused/${studyUid}/${usecase}/meta`),
  },
};

export interface FusedMeta {
  views: Record<"axial" | "coronal" | "sagittal", number>;
  defaults: Record<"axial" | "coronal" | "sagittal", number>;
  has_ct: boolean;
  has_lesions: boolean;
}

export function getFusedUrl(
  studyUid: string,
  usecase: string,
  view: "axial" | "coronal" | "sagittal"
): string {
  return `${API_BASE}/fused/${studyUid}/${usecase}/${view}`;
}

// Fused PET/CT for a specific slice of a view — used by the interactive viewer.
// `showLesions` toggles the cyan detected-lesion contour overlay (included in
// the URL so toggled views are cached independently).
export function getFusedSliceUrl(
  studyUid: string,
  usecase: string,
  view: "axial" | "coronal" | "sagittal",
  slice: number,
  showLesions: boolean = true
): string {
  return `${API_BASE}/fused/${studyUid}/${usecase}/${view}/${slice}?lesions=${showLesions}`;
}

export function getArtifactUrl(
  studyUid: string,
  usecase: string,
  artifactName: string
): string {
  // redirect=false: backend streams bytes directly instead of redirecting to
  // internal MinIO URL (http://minio:9000) which the browser cannot reach.
  return `${API_BASE}/artifacts/${studyUid}/${usecase}/${artifactName}?redirect=false`;
}

export function getPreviewUrl(
  studyUid: string,
  usecase: string,
  view: "axial" | "coronal" | "sagittal"
): string {
  return `${API_BASE}/preview/${studyUid}/${usecase}/${view}`;
}

export async function fetchHealth(): Promise<{ status: string; version: string }> {
  const res = await fetch("/health");
  if (!res.ok) throw new Error("Health check failed");
  return res.json();
}
