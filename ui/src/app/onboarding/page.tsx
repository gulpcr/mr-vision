"use client";

import { useCallback, useEffect, useState } from "react";
import { api, PatientRecordOut } from "@/lib/api";
import { CheckCircle2, AlertTriangle, UserPlus, Search, X, Pencil } from "lucide-react";

const SEX = ["female", "male", "other"];
const AGE_BANDS = ["0-17", "18-39", "40-64", "65+"];
// Study / modality types selectable at intake. The first group mirrors the active
// use-case plugins; the rest are general acquisition types.
const MODALITIES = [
  "Abdomen MRI", "Brain MRI", "Chest MRI", "Coronary CTA",
  "PET-CT", "PET-CT Brain", "Spine MRI",
  "Bilateral Mammogram", "Right Mammogram", "Left Mammogram",
  "CT Contrast", "CT Plain", "MRI Contrast", "MRI Plain",
];
const PRIORITIES = ["routine", "stat"];
const REGION_PROFILES = ["PK-diagnostic-assist", "AU-decision-support"];

const EMPTY = {
  patient_ref: "", sex: "", age_band: "", modality: "",
  indication: "", referrer: "", priority: "routine",
  region_profile: "PK-diagnostic-assist", study_instance_uid: "", consent_ack: false,
  clinical_history: "", comparative_study: "", height_cm: "", weight_kg: "",
  fasting_glucose: "", injection_site: "", creatinine: "",
};

/** BMI (kg/m²) from height (cm) + weight (kg); "" if either is missing/invalid. */
function calcBmi(heightCm: string, weightKg: string): string {
  const h = parseFloat(heightCm), w = parseFloat(weightKg);
  if (!h || !w || h <= 0 || w <= 0) return "";
  return (w / ((h / 100) ** 2)).toFixed(1);
}

export default function OnboardingPage() {
  const [form, setForm] = useState({ ...EMPTY });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [patients, setPatients] = useState<PatientRecordOut[]>([]);
  const [search, setSearch] = useState("");
  const [editPatientId, setEditPatientId] = useState<string | null>(null);
  const [editOrderId, setEditOrderId] = useState<string | null>(null);

  const loadPatients = useCallback(async (q = "") => {
    try { setPatients(await api.onboarding.searchPatients(q)); } catch { /* non-critical */ }
  }, []);

  useEffect(() => { loadPatients(); }, [loadPatients]);

  const set = (k: keyof typeof EMPTY, v: string | boolean) => setForm((f) => ({ ...f, [k]: v }));

  const resetForm = () => {
    setForm({ ...EMPTY });
    setEditPatientId(null);
    setEditOrderId(null);
    setError(null);
  };

  const startEdit = async (patientId: string) => {
    setError(null); setSuccess(null);
    try {
      const { patient, orders } = await api.onboarding.getPatient(patientId);
      const o = orders[0]; // most recent order, if any
      setForm({
        patient_ref: patient.patient_ref,
        sex: patient.sex || "",
        age_band: patient.age_band || "",
        modality: o?.modality || "",
        indication: o?.indication || "",
        referrer: o?.referrer || "",
        priority: o?.priority || "routine",
        region_profile: o?.region_profile || "PK-diagnostic-assist",
        study_instance_uid: o?.study_instance_uid || "",
        consent_ack: o?.consent_ack ?? false,
        clinical_history: o?.clinical_history || "",
        comparative_study: o?.comparative_study || "",
        height_cm: o?.height_cm != null ? String(o.height_cm) : "",
        weight_kg: o?.weight_kg != null ? String(o.weight_kg) : "",
        fasting_glucose: o?.fasting_glucose || "",
        injection_site: o?.injection_site || "",
        creatinine: o?.creatinine || "",
      });
      setEditPatientId(patient.id);
      setEditOrderId(o?.id || null);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e: any) {
      setError(e.message || "Failed to load patient");
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null); setSuccess(null); setSubmitting(true);
    const num = (s: string) => (s.trim() === "" ? null : Number(s));
    const clinical = {
      clinical_history: form.clinical_history.trim() || null,
      comparative_study: form.comparative_study.trim() || null,
      height_cm: num(form.height_cm),
      weight_kg: num(form.weight_kg),
      fasting_glucose: form.fasting_glucose.trim() || null,
      injection_site: form.injection_site.trim() || null,
      creatinine: form.creatinine.trim() || null,
    };
    try {
      if (editPatientId) {
        // Edit existing patient (+ their order).
        await api.onboarding.updatePatient(editPatientId, { sex: form.sex, age_band: form.age_band });
        const orderFields = {
          modality: form.modality,
          indication: form.indication.trim(), region_profile: form.region_profile,
          referrer: form.referrer.trim() || null, priority: form.priority,
          consent_ack: form.consent_ack,
          study_instance_uid: form.study_instance_uid.trim() || null,
          ...clinical,
        };
        if (editOrderId) {
          await api.onboarding.updateOrder(editOrderId, orderFields);
        } else {
          // Patient had no order yet → create one.
          await api.onboarding.createOrder({
            patient_ref: form.patient_ref.trim(), sex: form.sex, age_band: form.age_band,
            ...orderFields,
          });
        }
        setSuccess(`Updated ${form.patient_ref}`);
        resetForm();
        loadPatients(search);
      } else {
        const res = await api.onboarding.createOrder({
          patient_ref: form.patient_ref.trim(),
          sex: form.sex, age_band: form.age_band,
          modality: form.modality,
          indication: form.indication.trim(),
          region_profile: form.region_profile,
          referrer: form.referrer.trim() || null,
          priority: form.priority,
          consent_ack: form.consent_ack,
          study_instance_uid: form.study_instance_uid.trim() || null,
          ...clinical,
        });
        const linked = res.order.study_instance_uid;
        setSuccess(
          `Order created for ${res.patient.patient_ref}` +
          (linked ? ` — linked to study ${linked.slice(0, 24)}…` : " — no study linked yet (will auto-link by MRN)")
        );
        setForm({ ...EMPTY });
        loadPatients(search);
      }
    } catch (e: any) {
      setError(e.message || "Failed to save");
    } finally {
      setSubmitting(false);
    }
  };

  const inputCls = "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500";
  const labelCls = "block text-xs font-medium text-gray-600 mb-1";

  return (
    <div>
      <div className="mb-5">
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
          <UserPlus className="w-6 h-6 text-primary-600" /> Patient Intake
        </h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Record a patient&apos;s clinical data and order. The clinical details appear on the
          report and link to the patient&apos;s DICOM study by MRN.
        </p>
      </div>

      {success && (
        <div className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-xl px-4 py-3 mb-3">
          <CheckCircle2 className="w-4 h-4 text-green-500 shrink-0" />
          <p className="text-sm text-green-800 flex-1">{success}</p>
          <button onClick={() => setSuccess(null)} className="text-green-400 hover:text-green-600"><X className="w-4 h-4" /></button>
        </div>
      )}
      {error && (
        <div className="flex items-start gap-3 bg-red-50 border border-red-200 rounded-xl px-4 py-3 mb-3">
          <AlertTriangle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
          <p className="text-sm text-red-700 flex-1">{error}</p>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600"><X className="w-4 h-4" /></button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Intake form */}
        <form onSubmit={submit} className="lg:col-span-2 bg-white rounded-xl shadow-sm border border-gray-200 p-5 space-y-4">
          {editPatientId && (
            <div className="flex items-center justify-between bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
              <span className="text-xs text-blue-700">Editing <b>{form.patient_ref}</b></span>
              <button type="button" onClick={resetForm} className="text-xs font-medium text-blue-600 hover:underline">
                + New patient
              </button>
            </div>
          )}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className={labelCls}>Patient Ref / MRN *</label>
              <input className={`${inputCls} ${editPatientId ? "bg-gray-100 text-gray-500" : ""}`} value={form.patient_ref} onChange={(e) => set("patient_ref", e.target.value)} required readOnly={!!editPatientId} placeholder="e.g. PT-0123" />
            </div>
            <div>
              <label className={labelCls}>Sex *</label>
              <select className={inputCls} value={form.sex} onChange={(e) => set("sex", e.target.value)} required>
                <option value="">Select…</option>
                {SEX.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className={labelCls}>Age band *</label>
              <select className={inputCls} value={form.age_band} onChange={(e) => set("age_band", e.target.value)} required>
                <option value="">Select…</option>
                {AGE_BANDS.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className={labelCls}>Modality *</label>
              <select className={inputCls} value={form.modality} onChange={(e) => set("modality", e.target.value)} required>
                <option value="">Select…</option>
                {MODALITIES.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
            <div>
              <label className={labelCls}>Priority</label>
              <select className={inputCls} value={form.priority} onChange={(e) => set("priority", e.target.value)}>
                {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
          </div>

          <div>
            <label className={labelCls}>Indication / Clinical history *</label>
            <textarea className={`${inputCls} min-h-[80px]`} value={form.indication} onChange={(e) => set("indication", e.target.value)} required placeholder="Reason for study, relevant clinical history…" />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className={labelCls}>Referrer</label>
              <input className={inputCls} value={form.referrer} onChange={(e) => set("referrer", e.target.value)} placeholder="Referring physician / hospital" />
            </div>
            <div>
              <label className={labelCls}>Region profile *</label>
              <select className={inputCls} value={form.region_profile} onChange={(e) => set("region_profile", e.target.value)} required>
                {REGION_PROFILES.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
          </div>

          <div>
            <label className={labelCls}>Link to study UID (optional)</label>
            <input className={inputCls} value={form.study_instance_uid} onChange={(e) => set("study_instance_uid", e.target.value)} placeholder="Leave blank to auto-link by MRN" />
          </div>

          {/* Clinical detail that populates the PET-CT report */}
          <div className="pt-2 border-t border-gray-100">
            <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-3">Report detail (PET-CT)</p>
            <div className="space-y-4">
              <div>
                <label className={labelCls}>Clinical history</label>
                <textarea className={`${inputCls} min-h-[60px]`} value={form.clinical_history} onChange={(e) => set("clinical_history", e.target.value)} placeholder="Defaults to indication if left blank" />
              </div>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                <div><label className={labelCls}>Height (cm)</label><input className={inputCls} type="number" step="any" value={form.height_cm} onChange={(e) => set("height_cm", e.target.value)} /></div>
                <div><label className={labelCls}>Weight (kg)</label><input className={inputCls} type="number" step="any" value={form.weight_kg} onChange={(e) => set("weight_kg", e.target.value)} /></div>
                <div>
                  <label className={labelCls}>BMI (kg/m²)</label>
                  <input className={`${inputCls} bg-gray-100 text-gray-600`} value={calcBmi(form.height_cm, form.weight_kg)} readOnly placeholder="Auto-calculated" title="Auto-calculated from height and weight" />
                </div>
                <div><label className={labelCls}>Fasting glucose (mg/dl)</label><input className={inputCls} value={form.fasting_glucose} onChange={(e) => set("fasting_glucose", e.target.value)} /></div>
                <div><label className={labelCls}>Creatinine (mg/dl)</label><input className={inputCls} type="number" step="any" value={form.creatinine} onChange={(e) => set("creatinine", e.target.value)} /></div>
                <div><label className={labelCls}>Injection site</label><input className={inputCls} value={form.injection_site} onChange={(e) => set("injection_site", e.target.value)} placeholder="e.g. right antecubital" /></div>
              </div>
              <div>
                <label className={labelCls}>Comparative study</label>
                <input className={inputCls} value={form.comparative_study} onChange={(e) => set("comparative_study", e.target.value)} placeholder="e.g. prior PET-CT dated…" />
              </div>
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input type="checkbox" checked={form.consent_ack} onChange={(e) => set("consent_ack", e.target.checked)} className="w-4 h-4" />
            Consent acknowledged (required)
          </label>

          <button type="submit" disabled={submitting}
            className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 disabled:opacity-50 transition-colors">
            {submitting ? "Saving…" : editPatientId ? "Update Patient" : "Create Order"}
          </button>
        </form>

        {/* Recent patients */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Patients</h2>
          <div className="relative mb-3">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input className={`${inputCls} pl-9`} value={search}
              onChange={(e) => { setSearch(e.target.value); loadPatients(e.target.value); }}
              placeholder="Search MRN…" />
          </div>
          <div className="space-y-2 max-h-[460px] overflow-y-auto">
            {patients.length === 0 ? (
              <p className="text-sm text-gray-400">No patients yet.</p>
            ) : patients.map((p) => (
              <div key={p.id} className="flex items-center justify-between border border-gray-100 rounded-lg px-3 py-2">
                <div>
                  <p className="text-sm font-medium text-gray-800">{p.patient_ref}</p>
                  <p className="text-xs text-gray-400">{p.sex || "—"} · {p.age_band || "—"}</p>
                </div>
                <button onClick={() => startEdit(p.id)} className="inline-flex items-center gap-1 text-xs text-primary-600 hover:underline">
                  <Pencil className="w-3 h-3" /> Edit
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
