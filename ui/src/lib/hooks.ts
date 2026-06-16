"use client";

import useSWR from "swr";
import { api, fetchHealth } from "./api";

export function useStudies(params?: Record<string, string>) {
  return useSWR(["studies", params], () => api.studies.list(params), {
    refreshInterval: 15000,
  });
}

export function useStudy(uid: string | null) {
  return useSWR(uid ? ["study", uid] : null, () => api.studies.get(uid!));
}

export function useStudyJobs(uid: string | null) {
  return useSWR(
    uid ? ["jobs", uid] : null,
    () => api.jobs.listByStudy(uid!).then((d) => d.jobs),
    { refreshInterval: 10000 }
  );
}

export function useStudyResults(uid: string | null) {
  return useSWR(uid ? ["results", uid] : null, () =>
    api.results
      .listByStudy(uid!)
      .then((d) => d.results)
      .catch(() => [])
  );
}

export function useResult(uid: string | null, usecase: string | null) {
  return useSWR(uid && usecase ? ["result", uid, usecase] : null, () =>
    api.results.get(uid!, usecase!)
  );
}

export function useResultVersions(uid: string | null, usecase: string | null) {
  return useSWR(
    uid && usecase ? ["result-versions", uid, usecase] : null,
    () => api.results.listVersions(uid!, usecase!).then((d) => d.results)
  );
}

export function useUiSchema(usecase: string | null) {
  return useSWR(usecase ? ["ui-schema", usecase] : null, () =>
    api.usecases.getUiSchema(usecase!)
  );
}

export function useUsecases() {
  return useSWR("usecases", () => api.usecases.list().then((d) => d.usecases));
}

export function useOrthancStudies() {
  return useSWR("orthanc-studies", () => api.orthanc.listStudies());
}

export function useRoutingRules() {
  return useSWR("routing-rules", () => api.admin.getRoutingRules());
}

export function useSiteConfig() {
  return useSWR("site-config", () => api.admin.getSiteConfig());
}

export function useHealth() {
  return useSWR("health", fetchHealth, { refreshInterval: 30000 });
}

export function useAuditLogs(params?: Record<string, string>) {
  return useSWR(["audit", params], () => api.audit.list(params), {
    refreshInterval: 30000,
  });
}

export function useReviewQueue(params?: Record<string, string>) {
  return useSWR(["review", params], () => api.review.list(params), {
    refreshInterval: 15000,
  });
}

export function useAlertRules() {
  return useSWR("alert-rules", () => api.alerts.list().then((d) => d.rules));
}

export function useExperiments() {
  return useSWR("experiments", () => api.experiments.list().then((d) => d.experiments));
}

export function useRetentionPolicies() {
  return useSWR("retention", () => api.retention.list().then((d) => d.policies));
}

export function useBatches() {
  return useSWR("batches", () => api.batches.list().then((d) => d.batches), {
    refreshInterval: 10000,
  });
}

export function useBatch(id: string | null) {
  return useSWR(id ? ["batch", id] : null, () => api.batches.get(id!), {
    refreshInterval: 5000,
  });
}

export function useCriticalAlerts(params?: Record<string, string>) {
  return useSWR(
    ["critical-alerts", params],
    () => api.criticalAlerts.list(params).then((d) => d.alerts),
    { refreshInterval: 30000 }
  );
}

export function useCriticalAlertStats() {
  return useSWR("critical-alert-stats", () => api.criticalAlerts.stats(), {
    refreshInterval: 30000,
  });
}
