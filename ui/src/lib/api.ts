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

// Multipart upload — same auth + error handling as fetchAPI, but the browser must
// set the multipart Content-Type (with boundary) itself, so we never set it here.
async function fetchUpload<T>(path: string, body: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("auth_token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(`${API_BASE}${path}`, { method: "POST", headers, body });
  if (!res.ok) {
    if (res.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem("auth_token");
      localStorage.removeItem("user");
      if (!window.location.pathname.startsWith("/login")) {
        window.location.href = "/login";
      }
    }
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = error.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
        ? detail.map((d: any) => d.msg || JSON.stringify(d)).join("; ")
        : detail
        ? JSON.stringify(detail)
        : `API error: ${res.status}`;
    throw new Error(message);
  }
  return res.json();
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
    // Session expired / unauthenticated (jwt mode) → clear token and bounce to login.
    if (res.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem("auth_token");
      localStorage.removeItem("user");
      if (!window.location.pathname.startsWith("/login")) {
        window.location.href = "/login";
      }
    }
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
  if (res.status === 204) return undefined as T;
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
  // Reading workflow + turnaround
  reading_status?: string;
  assigned_to?: string | null;
  assigned_to_username?: string | null;
  assigned_at?: string | null;
  reported_at?: string | null;
  signed_at?: string | null;
  tat_report_minutes?: number | null;
  tat_signoff_minutes?: number | null;
  series: Series[];
  created_at: string;
  updated_at: string;
}

export interface PatientRecordOut {
  id: string;
  patient_ref: string;
  sex: string | null;
  age_band: string | null;
  created_at: string | null;
}

export interface OrderCreate {
  /** Referring Doctor's account — grants them (study.view.referred) access to the study. */
  referring_user_id?: string | null;
  patient_ref: string;
  sex: string;
  age_band: string;
  modality: string;
  body_part?: string | null;
  indication: string;
  region_profile: string;
  referrer?: string | null;
  priority?: string;
  consent_ack: boolean;
  study_instance_uid?: string | null;
  clinical_history?: string | null;
  comparative_study?: string | null;
  height_cm?: number | null;
  weight_kg?: number | null;
  fasting_glucose?: string | null;
  fasting_glucose_dt?: string | null;
  injection_site?: string | null;
  creatinine?: string | null;
  creatinine_dt?: string | null;
  external_order_ref?: string | null;
}

export interface ClinicalForStudy {
  indication?: string;
  clinical_history?: string;
  comparative_study?: string;
  referrer?: string | null;
  priority?: string;
  height_cm?: number | null;
  weight_kg?: number | null;
  bmi?: number | null;
  fasting_glucose?: string | null;
  injection_site?: string | null;
  creatinine?: string | null;
  sex?: string | null;
  age_band?: string | null;
  patient_ref?: string | null;
}

export interface MammographyReportData {
  study_instance_uid?: string;
  laterality?: string | null;
  file_no?: string | null;
  status?: string | null;
  contact?: string | null;
  procedure?: string | null;
  clinical_features?: string | null;
  right_breast_findings?: string | null;
  left_breast_findings?: string | null;
  opinion?: string | null;
  birads_right?: string | null;
  birads_left?: string | null;
  // Structured per-breast finding slots.
  density_right?: string | null;
  density_left?: string | null;
  mass_right?: string | null;
  mass_left?: string | null;
  calcification_right?: string | null;
  calcification_left?: string | null;
  skin_thickening_right?: string | null;
  skin_thickening_left?: string | null;
  nipple_retraction_right?: string | null;
  nipple_retraction_left?: string | null;
  architectural_distortion_right?: string | null;
  architectural_distortion_left?: string | null;
  axillary_nodes_right?: string | null;
  axillary_nodes_left?: string | null;
  reviewing_doctor?: string | null;
  reporting_doctor?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface OrderOut {
  referring_user_id?: string | null;
  id: string;
  patient_id: string;
  modality: string;
  body_part: string | null;
  referrer: string | null;
  priority: string;
  indication: string;
  region_profile: string;
  consent_ack: boolean;
  study_instance_uid: string | null;
  clinical_history?: string | null;
  comparative_study?: string | null;
  height_cm?: number | null;
  weight_kg?: number | null;
  bmi?: number | null;
  fasting_glucose?: string | null;
  // ISO timestamps for when each sample was drawn — feed the derived Observation's
  // effectiveDateTime instead of falling back to the intake time.
  fasting_glucose_dt?: string | null;
  injection_site?: string | null;
  creatinine?: string | null;
  creatinine_dt?: string | null;
  external_order_ref?: string | null;
  // Coded diagnosis, merged in from the order's FHIR Condition row by GET /patients/{id}.
  diagnosis_system?: string | null;
  diagnosis_code?: string | null;
  diagnosis_display?: string | null;
  created_at: string | null;
}

export interface ReadingState {
  study_instance_uid: string;
  reading_status: string;
  assigned_to: string | null;
  assigned_to_username: string | null;
  assigned_at: string | null;
  reported_at: string | null;
  signed_at: string | null;
  tat_report_minutes: number | null;
  tat_signoff_minutes: number | null;
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

export interface SliceWindow {
  name: string;
  level: number;
  width: number;
}

/** A CT-report slice tile resolved to the exact DICOM image the viewer shows. */
export interface SliceLinkTile {
  artifact_name: string;
  z: number;
  window: SliceWindow | null;
  sop_instance_uid: string;
  instance_number: number | null;
  stack_position: number | null;
  /** Got the detailed (Pass-2) read and appears in the report. */
  reported: boolean;
  /** Evenly-spread overview tile, stored whether flagged or not. */
  overview: boolean;
  /** Flagged by the Pass-1 screening scan (may not have been reviewed in detail). */
  screen_flagged: boolean;
  finding: string | null;
}

export interface FlaggedImage {
  z: number;
  sop_instance_uid: string;
  instance_number: number | null;
  stack_position: number | null;
  finding: string | null;
  /** false → screening-only flag, not covered by the detailed read. */
  reported: boolean;
}

export interface FlaggedSlices {
  supported: boolean;
  resolved: boolean;
  reason: string | null;
  series_instance_uid: string | null;
  series_description: string | null;
  spacing_irregular: boolean;
  tiles: SliceLinkTile[];
  flagged_images: FlaggedImage[];
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

export interface UserResponse {
  id: string;
  username: string;
  email: string;
  full_name: string;
  role: string;
  tenant_id: string;
  is_active: boolean;
  is_platform_admin?: boolean;
  is_platform_operator?: boolean;
  totp_enabled?: boolean;
  created_at: string | null;
  status?: string;
}

// ── Workspace (tenant) / RBAC / dashboards / platform console ────────────────

export interface MyPermissions {
  user_id: string;
  role: string | null;
  tenant_id: string | null;
  permissions: string[];
  referral_scoped: boolean;
  is_platform_admin: boolean;
  is_platform_operator: boolean;
  is_admin: boolean;
}

export interface WorkspaceBranding {
  display_name: string | null;
  logo_data_url: string | null;
  primary_color: string | null;
  accent_color: string | null;
}

export interface WorkspaceSettings {
  institution_name: string | null;
  institution_address: string | null;
  report_header: string | null;
  report_footer: string | null;
  signatory_name: string | null;
  signatory_title: string | null;
  signatory_qualifications: string | null;
  secondary_signatory_name: string | null;
  timezone: string | null;
}

export interface WorkspaceInfo {
  tenant_id: string;
  workspace: string;
  plan: string | null;
  features: string[];
  branding: WorkspaceBranding;
  settings: WorkspaceSettings;
}

export interface RoleDef {
  id: string;
  name: string;
  permissions: string[];
  is_system: boolean;
  user_count?: number;
}

export interface InviteResult extends UserResponse {
  invite_link: string;
}

export interface WidgetPosition { x: number; y: number; w: number; h: number }

export interface DashboardWidget {
  id: string;
  type: string;
  title: string;
  config: Record<string, any>;
  position: WidgetPosition;
}

export interface Dashboard {
  id: string;
  name: string;
  description: string | null;
  owner_id: string | null;
  is_default: boolean;
  role_default: string | null;
  is_shared: boolean;
  widgets: DashboardWidget[];
  filters: Record<string, any>;
  refresh_interval: number;
  can_edit: boolean;
  is_owner: boolean;
  updated_at: string | null;
}

export interface WidgetConfigField {
  key: string;
  label: string;
  type: "select" | "number" | "string" | "text";
  default: any;
  options?: { value: string; label: string }[];
  min?: number;
  max?: number;
}

export interface WidgetType {
  type: string;
  label: string;
  description: string;
  category: string;
  default_size: { w: number; h: number };
  config_schema: WidgetConfigField[];
  personal?: boolean;
}

export interface WidgetData {
  type: string;
  data: any;
  error?: string | null;
  fetched_at?: number;
}

export interface DashboardVersion {
  version: number;
  created_by: string | null;
  created_at: string | null;
  name: string | null;
  widget_count: number;
}

export interface PlatformTenantRow {
  tenant_id: string;
  name: string;
  slug: string;
  status: string;
  plan: string;
  max_users: number | null;
  deleted_at: string | null;
  users: number;
  studies: number;
  studies_30d: number;
  jobs_failed_7d: number;
  jobs_active: number;
  storage_bytes: number;
  last_study_at: string | null;
}

export interface PlatformOverview {
  tenants: PlatformTenantRow[];
  totals: Record<string, number>;
}

export interface PlatformAuditEntry {
  id: string;
  tenant_id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  actor: string;
  outcome: string | null;
  client_ip: string | null;
  details: Record<string, any>;
  seq: number | null;
  timestamp: string | null;
}

export interface PlatformUser {
  id: string;
  username: string;
  email: string;
  full_name: string;
  tenant_id: string;
  role: string;
  is_active: boolean;
  status: string;
  totp_enabled: boolean;
  is_platform_admin: boolean;
  created_at: string | null;
}

export interface DicomEndpoint {
  id: string;
  tenant_id: string;
  called_aet: string;
  calling_aet: string | null;
  description: string | null;
  is_active: boolean;
  created_at: string | null;
}

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  status: string;
  plan: string;
  features: string[];
  created_at: string | null;
}

export interface CreatedTenant extends Tenant {
  admin_username: string;
  admin_temp_password: string | null;
  admin_invite_link: string | null;
  called_aet: string | null;
}

export interface TenantApiKey {
  id: string;
  tenant_id: string;
  name: string;
  prefix: string;
  scopes: string[];
  expires_at: string | null;
  is_active: boolean;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string | null;
}

export interface CreatedTenantApiKey extends TenantApiKey {
  key: string;
}

export interface TenantUser {
  id: string;
  username: string;
  email: string;
  full_name: string;
  role: string;
  tenant_id: string;
  is_active: boolean;
  is_platform_admin: boolean;
  is_platform_operator: boolean;
  totp_enabled: boolean;
  created_at: string | null;
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

// Returned by GET /results/{id}/shares — token is truncated server-side, so
// this is a distinct (narrower) shape from ShareLink, not the same object.
export interface ShareLinkSummary {
  id: string;
  token: string;
  expires_at: string | null;
  is_active: boolean;
  created_by: string;
  created_at: string | null;
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

export interface DicomUploadResult {
  uploaded: number;
  failed: number;
  studies_ingested: Array<{
    study_instance_uid: string;
    patient_name?: string | null;
    patient_id?: string | null;
    modality?: string | null;
    series_count?: number;
    error?: string;
  }>;
  files: Array<{
    filename: string;
    status: "uploaded" | "error" | "skipped";
    detail?: string;
    study_instance_uid?: string;
  }>;
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
    // Upload raw DICOM files (or a folder) straight from the browser. Ingests
    // every distinct study the files belong to. Upload in batches — a study can
    // be thousands of instances.
    upload: (files: File[]) => {
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      return fetchUpload<DicomUploadResult>("/studies/upload", fd);
    },
  },
  onboarding: {
    referringDoctors: () =>
      fetchAPI<{ id: string; username: string; full_name: string }[]>("/referring-doctors"),
    searchPatients: (query = "") =>
      fetchAPI<PatientRecordOut[]>(`/patients?query=${encodeURIComponent(query)}`),
    getPatient: (id: string) =>
      fetchAPI<{ patient: PatientRecordOut; orders: OrderOut[] }>(`/patients/${id}`),
    getClinical: (studyUid: string) =>
      fetchAPI<ClinicalForStudy>(`/studies/${studyUid}/clinical`),
    createOrder: (payload: OrderCreate) =>
      fetchAPI<{ order: OrderOut; patient: PatientRecordOut }>("/orders", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    updatePatient: (id: string, body: { sex?: string; age_band?: string }) =>
      fetchAPI<PatientRecordOut>(`/patients/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    updateOrder: (id: string, body: Partial<OrderCreate>) =>
      fetchAPI<OrderOut>(`/orders/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    linkStudy: (orderId: string, studyUid: string) =>
      fetchAPI<OrderOut>(
        `/orders/${orderId}/link-study?study_uid=${encodeURIComponent(studyUid)}`,
        { method: "POST" }
      ),
  },
  reading: {
    claim: (uid: string) =>
      fetchAPI<ReadingState>(`/studies/${uid}/claim`, { method: "POST" }),
    autoAssign: (uid: string) =>
      fetchAPI<ReadingState>(`/studies/${uid}/auto-assign`, { method: "POST" }),
    assign: (uid: string, assigneeId: string) =>
      fetchAPI<ReadingState>(
        `/studies/${uid}/assign?assignee_id=${encodeURIComponent(assigneeId)}`,
        { method: "POST" }
      ),
    unclaim: (uid: string) =>
      fetchAPI<ReadingState>(`/studies/${uid}/unclaim`, { method: "POST" }),
    report: (uid: string) =>
      fetchAPI<ReadingState>(`/studies/${uid}/report`, { method: "POST" }),
    sign: (uid: string) =>
      fetchAPI<ReadingState>(`/studies/${uid}/sign`, { method: "POST" }),
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
    flaggedSlices: (studyUid: string, usecase: string) =>
      fetchAPI<FlaggedSlices>(`/results/${studyUid}/${usecase}/flagged-slices`),
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
      fetchAPI<{ routing_rules: Record<string, any[]>; site_overrides: any[] }>("/admin/routing-rules"),
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
    deleteStudy: (orthancId: string) =>
      fetchAPI<void>(`/orthanc/studies/${orthancId}`, { method: "DELETE" }),
  },
  auth: {
    login: (username: string, password: string, workspace?: string) =>
      fetchAPI<{
        mfa_required: boolean;
        mfa_token: string | null;
        access_token: string | null;
        token_type: string;
        user_id: string | null;
        username: string | null;
        role: string | null;
        tenant_id: string | null;
      }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password, workspace: workspace || undefined }),
      }),
    me: () => fetchAPI<UserResponse & { tenant_slug?: string }>("/auth/me"),
    myPermissions: () => fetchAPI<MyPermissions>("/auth/me/permissions"),
    inviteUser: (data: { username: string; email: string; role: string; full_name?: string }) =>
      fetchAPI<InviteResult>("/auth/users/invite", { method: "POST", body: JSON.stringify(data) }),
    reinviteUser: (userId: string) =>
      fetchAPI<{ invite_link: string }>(`/auth/users/${userId}/reinvite`, { method: "POST" }),
    revokeUserSessions: (userId: string) =>
      fetchAPI<{ status: string }>(`/auth/users/${userId}/revoke-sessions`, { method: "POST" }),
    acceptInvitation: (token: string, password: string) =>
      fetchAPI<{ status: string; username: string; tenant_id: string }>("/auth/invitations/accept", {
        method: "POST",
        body: JSON.stringify({ token, password }),
      }),
    // (Re)issue / drop the httpOnly viewer-session cookie that authorises the OHIF
    // viewer's DICOMweb requests and the realtime WebSocket for the current tenant.
    refreshViewerSession: () => fetchAPI<void>("/auth/viewer-session", { method: "POST" }),
    clearViewerSession: () => fetchAPI<void>("/auth/viewer-session", { method: "DELETE" }),
    register: (data: { username: string; email: string; password: string; full_name?: string }) =>
      fetchAPI<UserResponse>("/auth/register", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    listUsers: (tenantId?: string) => {
      const qs = tenantId ? `?${new URLSearchParams({ tenant_id: tenantId }).toString()}` : "";
      return fetchAPI<TenantUser[]>(`/auth/users${qs}`);
    },
    updateUserRole: (userId: string, role: string) =>
      fetchAPI(`/auth/users/${userId}/role?${new URLSearchParams({ role }).toString()}`, {
        method: "PUT",
      }),
    deactivateUser: (userId: string) =>
      fetchAPI(`/auth/users/${userId}`, { method: "DELETE" }),
    updatePlatformAdmin: (userId: string, isPlatformAdmin: boolean) =>
      fetchAPI(`/auth/users/${userId}/platform-admin`, {
        method: "PUT",
        body: JSON.stringify({ is_platform_admin: isPlatformAdmin }),
      }),
    updatePlatformOperator: (userId: string, isPlatformOperator: boolean) =>
      fetchAPI(`/auth/users/${userId}/platform-operator`, {
        method: "PUT",
        body: JSON.stringify({ is_platform_operator: isPlatformOperator }),
      }),
    impersonate: (userId: string) =>
      fetchAPI<{
        access_token: string;
        token_type: string;
        user_id: string;
        username: string;
        role: string;
        tenant_id: string;
        expires_in_minutes: number;
      }>(`/auth/users/${userId}/impersonate`, { method: "POST" }),
    stopImpersonation: () => fetchAPI<{ status: string }>("/auth/impersonate/stop", { method: "POST" }),
    mfaVerify: (mfaToken: string, code: string) =>
      fetchAPI<{
        mfa_required: boolean;
        access_token: string | null;
        token_type: string;
        user_id: string | null;
        username: string | null;
        role: string | null;
        tenant_id: string | null;
      }>("/auth/mfa/verify", {
        method: "POST",
        body: JSON.stringify({ mfa_token: mfaToken, code }),
      }),
    mfaEnroll: () =>
      fetchAPI<{ secret: string; otpauth_uri: string; qr_code_data_uri: string }>("/auth/mfa/enroll", {
        method: "POST",
      }),
    mfaConfirm: (code: string) =>
      fetchAPI<{ recovery_codes: string[] }>("/auth/mfa/confirm", {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
    mfaDisable: (code: string) =>
      fetchAPI<{ status: string }>("/auth/mfa/disable", {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
  },
  tenants: {
    list: () => fetchAPI<Tenant[]>("/admin/tenants"),
    get: (id: string) => fetchAPI<Tenant>(`/admin/tenants/${id}`),
    create: (data: {
      name: string;
      slug: string;
      plan?: string;
      features?: string[];
      admin_username: string;
      admin_email: string;
      admin_full_name?: string;
      called_aet?: string;
      max_users?: number;
    }) => fetchAPI<CreatedTenant>("/admin/tenants", { method: "POST", body: JSON.stringify(data) }),
    listDicomEndpoints: (id: string) => fetchAPI<DicomEndpoint[]>(`/admin/tenants/${id}/dicom-endpoints`),
    createDicomEndpoint: (id: string, data: { called_aet: string; calling_aet?: string; description?: string }) =>
      fetchAPI<DicomEndpoint>(`/admin/tenants/${id}/dicom-endpoints`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    deleteDicomEndpoint: (id: string, endpointId: string) =>
      fetchAPI<void>(`/admin/tenants/${id}/dicom-endpoints/${endpointId}`, { method: "DELETE" }),
    updatePlan: (id: string, plan: string) =>
      fetchAPI<Tenant>(`/admin/tenants/${id}/plan`, { method: "PUT", body: JSON.stringify({ plan }) }),
    updateFeatures: (id: string, features: string[]) =>
      fetchAPI<Tenant>(`/admin/tenants/${id}/features`, {
        method: "PUT",
        body: JSON.stringify({ features }),
      }),
    updateStatus: (id: string, status: string) =>
      fetchAPI<Tenant>(`/admin/tenants/${id}/status`, { method: "PUT", body: JSON.stringify({ status }) }),
  },
  tenant: {
    current: () => fetchAPI<WorkspaceInfo>("/tenant/current"),
    publicBranding: (workspace?: string) =>
      fetchAPI<WorkspaceBranding & { workspace: string | null }>(
        `/tenant/public-branding${workspace ? `?${new URLSearchParams({ workspace }).toString()}` : ""}`,
      ),
    updateSettings: (data: Partial<WorkspaceSettings>) =>
      fetchAPI<WorkspaceSettings>("/tenant/settings", { method: "PUT", body: JSON.stringify(data) }),
    updateBranding: (data: Partial<WorkspaceBranding>) =>
      fetchAPI<WorkspaceBranding>("/tenant/branding", { method: "PUT", body: JSON.stringify(data) }),
  },
  roles: {
    list: () => fetchAPI<RoleDef[]>("/roles"),
    catalog: () => fetchAPI<{ permissions: { key: string; description: string }[] }>("/roles/permissions"),
    create: (data: { name: string; permissions: string[] }) =>
      fetchAPI<RoleDef>("/roles", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: { name?: string; permissions?: string[] }) =>
      fetchAPI<RoleDef>(`/roles/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    remove: (id: string) => fetchAPI<void>(`/roles/${id}`, { method: "DELETE" }),
  },
  dashboards: {
    list: () => fetchAPI<Dashboard[]>("/dashboards"),
    widgetTypes: () => fetchAPI<WidgetType[]>("/dashboards/widget-types"),
    get: (id: string) => fetchAPI<Dashboard>(`/dashboards/${id}`),
    create: (data: Partial<Dashboard>) =>
      fetchAPI<Dashboard>("/dashboards", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<Dashboard>) =>
      fetchAPI<Dashboard>(`/dashboards/${id}`, { method: "PUT", body: JSON.stringify(data) }),
    remove: (id: string) => fetchAPI<void>(`/dashboards/${id}`, { method: "DELETE" }),
    clone: (id: string, name?: string) =>
      fetchAPI<Dashboard>(`/dashboards/${id}/clone`, { method: "POST", body: JSON.stringify({ name }) }),
    versions: (id: string) => fetchAPI<DashboardVersion[]>(`/dashboards/${id}/versions`),
    restore: (id: string, version: number) =>
      fetchAPI<Dashboard>(`/dashboards/${id}/versions/${version}/restore`, { method: "POST" }),
    data: (id: string, filters?: Record<string, any>) =>
      fetchAPI<{ data: Record<string, WidgetData> }>(`/dashboards/${id}/data`, {
        method: "POST",
        body: JSON.stringify({ filters: filters ?? null }),
      }),
  },
  platform: {
    overview: () => fetchAPI<PlatformOverview>("/admin/platform/overview"),
    audit: (params: { tenant_id?: string; action?: string; actor?: string; limit?: number; offset?: number }) => {
      const qs = new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]),
      ).toString();
      return fetchAPI<PlatformAuditEntry[]>(`/admin/platform/audit${qs ? `?${qs}` : ""}`);
    },
    users: (q: string, tenantId?: string) => {
      const qs = new URLSearchParams({ q, ...(tenantId ? { tenant_id: tenantId } : {}) }).toString();
      return fetchAPI<PlatformUser[]>(`/admin/platform/users?${qs}`);
    },
    revokeSessions: (userId: string) =>
      fetchAPI<{ status: string }>(`/admin/platform/users/${userId}/revoke-sessions`, { method: "POST" }),
    resetLink: (userId: string) =>
      fetchAPI<{ reset_link: string }>(`/admin/platform/users/${userId}/reset-link`, { method: "POST" }),
    updateLimits: (tenantId: string, maxUsers: number | null) =>
      fetchAPI<{ tenant_id: string; max_users: number | null }>(`/admin/platform/tenants/${tenantId}/limits`, {
        method: "PUT",
        body: JSON.stringify({ max_users: maxUsers }),
      }),
    purgeTenant: (tenantId: string, confirmSlug: string) =>
      fetchAPI<Record<string, any>>(
        `/admin/platform/tenants/${tenantId}/purge?${new URLSearchParams({ confirm: confirmSlug }).toString()}`,
        { method: "POST" },
      ),
  },
  tenantApiKeys: {
    list: (tenantId: string) => fetchAPI<TenantApiKey[]>(`/admin/tenants/${tenantId}/api-keys`),
    create: (tenantId: string, data: { name: string; scopes: string[]; expires_at?: string }) =>
      fetchAPI<CreatedTenantApiKey>(`/admin/tenants/${tenantId}/api-keys`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    revoke: (tenantId: string, keyId: string) =>
      fetchAPI<TenantApiKey>(`/admin/tenants/${tenantId}/api-keys/${keyId}`, { method: "DELETE" }),
  },
  plans: {
    list: () => fetchAPI<Record<string, string[]>>("/admin/plans"),
    getFeatures: (planName: string) =>
      fetchAPI<{ plan_name: string; features: string[] }>(`/admin/plans/${planName}/features`),
    setFeatures: (planName: string, features: string[]) =>
      fetchAPI<{ plan_name: string; features: string[] }>(`/admin/plans/${planName}/features`, {
        method: "PUT",
        body: JSON.stringify({ features }),
      }),
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
    downloadPdfBlob: (studyUid: string, usecase: string) =>
      fetchBlob(`/reports/${studyUid}/${usecase}/pdf`),
    consolidatedReport: (studyUid: string, usecase: string) =>
      fetchAPI<{
        findings: string;
        conclusions: string;
        model: string | null;
        grounded: boolean;
        flagged_count: number;
      }>(`/reports/${studyUid}/${usecase}/consolidated-report`),
    getSrUrl: (studyUid: string, usecase: string) =>
      `${API_BASE}/reports/${studyUid}/${usecase}/dicom-sr`,
    getFhirUrl: (studyUid: string, usecase: string) =>
      `${API_BASE}/reports/${studyUid}/${usecase}/fhir`,
  },
  mammography: {
    getReport: (studyUid: string) =>
      fetchAPI<MammographyReportData>(`/studies/${studyUid}/mammography-report`),
    saveReport: (studyUid: string, data: Partial<MammographyReportData>) =>
      fetchAPI<MammographyReportData>(`/studies/${studyUid}/mammography-report`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    downloadPdfBlob: (studyUid: string) =>
      fetchBlob(`/studies/${studyUid}/mammography-report.pdf`),
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
    listShares: (resultId: string) =>
      fetchAPI<{ shares: ShareLinkSummary[] }>(`/results/${resultId}/shares`),
    revokeShare: (linkId: string) =>
      fetchAPI(`/portal/shares/${linkId}/revoke`, { method: "POST" }),
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
  medgemmaDebug: {
    // Reconstruct the exact MedGemma inputs (prompt + all images) for a study and,
    // when run=true, re-run the local model and return raw + parsed output.
    get: (
      usecase: string,
      studyUid: string,
      opts?: { run?: boolean; sendAll?: boolean; includeImages?: boolean }
    ) => {
      const p = new URLSearchParams();
      p.set("run", String(opts?.run ?? false));
      if (opts?.sendAll) p.set("send_all", "true");
      if (opts?.includeImages) p.set("include_images", "true");
      return fetchAPI<MedGemmaDebug>(
        `/debug/medgemma/${usecase}/${encodeURIComponent(studyUid)}?${p.toString()}`
      );
    },
  },
};

export interface FusedMeta {
  views: Record<"axial" | "coronal" | "sagittal", number>;
  defaults: Record<"axial" | "coronal" | "sagittal", number>;
  has_ct: boolean;
  has_lesions: boolean;
}

export interface MedGemmaDebugImage {
  name: string;
  artifact_type: string | null;
  bytes: number;
  sha256: string;
  sent_to_model: boolean;
  base64?: string;
}

export interface MedGemmaDebug {
  usecase: string;
  study_uid: string;
  result_version: number | null;
  stored_ai_report_provider: string | null;
  medgemma: {
    enabled: boolean;
    base_url: string;
    model: string;
    force_json: boolean;
    ready?: boolean;
  };
  inputs: {
    prompt: string;
    prompt_chars: number;
    n_images_total: number;
    n_images_sent: number;
    send_all: boolean;
    sent_image_names: string[];
    images: MedGemmaDebugImage[];
  };
  output: {
    raw: string;
    raw_chars: number;
    parsed: unknown;
    parsed_ok: boolean;
    parse_error: string | null;
    elapsed_s: number;
  } | null;
  note?: string;
}

export function getFusedUrl(
  studyUid: string,
  usecase: string,
  view: "axial" | "coronal" | "sagittal"
): string {
  return `${API_BASE}/fused/${studyUid}/${usecase}/${view}`;
}

// PET/CT for a specific slice of a view — used by the interactive viewer.
// `showLesions` toggles the cyan detected-lesion contour overlay (included in
// the URL so toggled views are cached independently). `mode` selects the render:
// "fused" (CT + PET overlay), "ct" (CT grayscale), or "pet" (PET colormap only).
export type FusedMode = "fused" | "ct" | "pet";

// Bump when the server-side slice rendering changes (colormap, grayscale, etc.)
// so the browser's 1h slice cache is bypassed instead of serving stale PNGs.
const FUSED_RENDER_VERSION = 4;

export function getFusedSliceUrl(
  studyUid: string,
  usecase: string,
  view: "axial" | "coronal" | "sagittal",
  slice: number,
  showLesions: boolean = true,
  mode: FusedMode = "fused"
): string {
  return `${API_BASE}/fused/${studyUid}/${usecase}/${view}/${slice}?lesions=${showLesions}&mode=${mode}&r=${FUSED_RENDER_VERSION}`;
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
