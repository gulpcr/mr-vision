"use client";

import { useEffect, useState } from "react";

// Module-level, ref-counted, app-wide cache of object URLs for JWT-protected
// images (a plain <img src> never sends the Authorization header, so every
// artifact/preview/fused-render fetch must go through this). Ref-counted so two
// mounted <AuthImg>s pointing at the same URL share one blob and only revoke it
// once the last consumer unmounts.
//
// Known limitation: if two consumers request the *same*, not-yet-cached URL in
// the same tick, both will independently fetch and the second response
// overwrites the first's cache entry (the first blob leaks, ref-count is briefly
// wrong). Acceptable for this app's usage (ReportView/ComparePanel render
// distinct URLs per view) — do not reuse this hook for a case where many
// consumers race on one identical URL without checking that assumption still holds.
interface CacheEntry {
  url: string;
  refCount: number;
}

const CACHE_LIMIT = 60;
const cache = new Map<string, CacheEntry>();

function touch(key: string, entry: CacheEntry) {
  cache.delete(key);
  cache.set(key, entry);
}

function evictIfNeeded() {
  if (cache.size <= CACHE_LIMIT) return;
  for (const key of Array.from(cache.keys())) {
    if (cache.size <= CACHE_LIMIT) break;
    const entry = cache.get(key);
    if (entry && entry.refCount <= 0) {
      URL.revokeObjectURL(entry.url);
      cache.delete(key);
    }
  }
}

export interface UseAuthenticatedImageResult {
  objectUrl: string | null;
  loading: boolean;
  error: boolean;
}

export function useAuthenticatedImage(src: string | null): UseAuthenticatedImageResult {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!src);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!src) {
      setObjectUrl(null);
      setLoading(false);
      setError(false);
      return;
    }

    let cancelled = false;
    setObjectUrl(null);
    setLoading(true);
    setError(false);

    const existing = cache.get(src);
    if (existing) {
      existing.refCount += 1;
      touch(src, existing);
      setObjectUrl(existing.url);
      setLoading(false);
    } else {
      const token = typeof window !== "undefined" ? localStorage.getItem("auth_token") : null;
      fetch(src, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
        .then((r) => {
          if (!r.ok) throw new Error(String(r.status));
          return r.blob();
        })
        .then((blob) => {
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          cache.set(src, { url, refCount: 1 });
          evictIfNeeded();
          setObjectUrl(url);
          setLoading(false);
        })
        .catch(() => {
          if (!cancelled) {
            setError(true);
            setLoading(false);
          }
        });
    }

    return () => {
      cancelled = true;
      const entry = cache.get(src);
      if (entry) {
        entry.refCount -= 1;
        evictIfNeeded();
      }
    };
  }, [src]);

  return { objectUrl, loading, error };
}
