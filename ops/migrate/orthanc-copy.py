#!/usr/bin/env python3
"""Copy every DICOM instance from an old Orthanc into the new, encrypted one — verified.

Used when moving to the production stack (Orthanc storage moves from files on disk to
client-side-encrypted objects in MinIO, the index from SQLite to Postgres): the new
Orthanc cannot read the old storage, so each instance is re-sent through the REST API,
which stores it encrypted. Standard library only; runs anywhere Python 3.8+ is.

    ORTHANC_SRC_PASSWORD=... ORTHANC_DST_PASSWORD=... \\
    python3 ops/migrate/orthanc-copy.py \\
        --src http://127.0.0.1:8042 --src-user orthanc \\
        --dst https://127.0.0.1:18042 --dst-user orthanc --dst-ca /srv/mrcv/pki/ca.crt \\
        --state /srv/mrcv/backups/orthanc-copy.state

  * resumable: instance ids already copied are recorded in --state; re-run after an
    interruption and it continues;
  * verified: each copy is read back from the new Orthanc and must be byte-identical
    (SHA-256) to the original; Orthanc study labels are copied too;
  * read-only on the source; never deletes anything;
  * --verify-only re-checks counts and a random sample without copying.

Passwords come from the environment (never the command line). Tunnel the old Orthanc
over SSH (ssh -L 18042:127.0.0.1:8042 old-host) when it is on another machine: the copy
then travels encrypted end to end. Exit status 0 only if every instance verified.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request


class Orthanc:
    def __init__(self, url: str, user: str, password: str, ca: str | None, insecure_ok: bool):
        self.url = url.rstrip("/")
        if self.url.startswith("http://") and not insecure_ok and not _is_loopback(self.url):
            sys.exit(f"refusing plain http to a non-local Orthanc ({self.url}); use https or an SSH tunnel")
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    def request(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None,
                retries: int = 4) -> bytes:
        headers = {"Authorization": self._auth}
        if ctype:
            headers["Content-Type"] = ctype
        for attempt in range(retries):
            try:
                req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, context=self._ctx, timeout=300) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == retries - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == retries - 1:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def json(self, path: str):
        return json.loads(self.request("GET", path))


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "[::1]")


def all_instances(src: Orthanc, page: int = 1000):
    since = 0
    while True:
        batch = src.json(f"/instances?since={since}&limit={page}")
        if not batch:
            return
        yield from batch
        since += len(batch)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--src-user", default="orthanc")
    ap.add_argument("--src-ca")
    ap.add_argument("--dst", required=True)
    ap.add_argument("--dst-user", default="orthanc")
    ap.add_argument("--dst-ca")
    ap.add_argument("--state", default="orthanc-copy.state")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--sample", type=int, default=200, help="instances re-hashed by --verify-only")
    ap.add_argument("--allow-insecure-remote", action="store_true",
                    help="permit plain http to a non-loopback Orthanc (not recommended)")
    a = ap.parse_args()

    src = Orthanc(a.src, a.src_user, os.environ.get("ORTHANC_SRC_PASSWORD", ""), a.src_ca, a.allow_insecure_remote)
    dst = Orthanc(a.dst, a.dst_user, os.environ.get("ORTHANC_DST_PASSWORD", ""), a.dst_ca, a.allow_insecure_remote)

    if a.verify_only:
        return verify(src, dst, a.sample)

    done: set[str] = set()
    if os.path.exists(a.state):
        with open(a.state) as f:
            done = {line.strip() for line in f if line.strip()}
    lock = threading.Lock()
    state = open(a.state, "a", buffering=1)
    stats = {"copied": 0, "skipped": len(done), "failed": 0}
    failures: list[str] = []

    def copy(iid: str) -> None:
        data = src.request("GET", f"/instances/{iid}/file")
        digest = hashlib.sha256(data).hexdigest()
        res = json.loads(dst.request("POST", "/instances", data, "application/dicom"))
        new_id = res.get("ID")
        if res.get("Status") not in ("Success", "AlreadyStored") or not new_id:
            raise RuntimeError(f"store failed: {res}")
        back = dst.request("GET", f"/instances/{new_id}/file")
        if hashlib.sha256(back).hexdigest() != digest:
            raise RuntimeError("read-back differs from the original")
        with lock:
            state.write(iid + "\n")
            stats["copied"] += 1

    todo = (i for i in all_instances(src) if i not in done)
    started = time.time()
    with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {}
        for iid in todo:
            futures[pool.submit(copy, iid)] = iid
            if len(futures) >= a.workers * 8:
                _drain(futures, stats, failures, wait_all=False)
        _drain(futures, stats, failures, wait_all=True)
    state.close()

    labels = copy_labels(src, dst)
    elapsed = time.time() - started
    print(f"copied {stats['copied']}, already done {stats['skipped']}, failed {stats['failed']}, "
          f"study labels {labels}, {elapsed:.0f}s")
    for f in failures[:20]:
        print("  FAILED", f)
    rc = verify(src, dst, sample=0)
    return 1 if stats["failed"] or rc else 0


def _drain(futures: dict, stats: dict, failures: list, wait_all: bool) -> None:
    pending = list(futures)
    done_iter = cf.as_completed(pending) if wait_all else [next(cf.as_completed(pending))]
    for fut in done_iter:
        iid = futures.pop(fut)
        try:
            fut.result()
        except Exception as exc:  # noqa: BLE001 - report and continue; re-run resumes
            stats["failed"] += 1
            failures.append(f"{iid}: {exc}")
        total = stats["copied"] + stats["failed"]
        if total and total % 500 == 0:
            print(f"  ... {stats['copied']} copied, {stats['failed']} failed", flush=True)


def copy_labels(src: Orthanc, dst: Orthanc) -> int:
    n = 0
    for sid in src.json("/studies"):
        try:
            for label in src.json(f"/studies/{sid}/labels"):
                dst.request("PUT", f"/studies/{sid}/labels/{label}", b"")
                n += 1
        except urllib.error.HTTPError:
            continue
    return n


def verify(src: Orthanc, dst: Orthanc, sample: int) -> int:
    s, d = src.json("/statistics"), dst.json("/statistics")
    keys = ("CountPatients", "CountStudies", "CountSeries", "CountInstances")
    ok = all(int(d[k]) >= int(s[k]) for k in keys)
    print("counts  source: " + ", ".join(f"{k[5:]} {s[k]}" for k in keys))
    print("        target: " + ", ".join(f"{k[5:]} {d[k]}" for k in keys))
    bad = 0
    if sample:
        ids = list(all_instances(src))
        for iid in random.sample(ids, min(sample, len(ids))):
            a = hashlib.sha256(src.request("GET", f"/instances/{iid}/file")).hexdigest()
            try:
                b = hashlib.sha256(dst.request("GET", f"/instances/{iid}/file")).hexdigest()
            except urllib.error.HTTPError:
                b = None
            bad += a != b
        print(f"sample  {min(sample, len(ids))} instances re-hashed, {bad} differ")
    print("VERIFIED" if ok and not bad else "NOT VERIFIED")
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
