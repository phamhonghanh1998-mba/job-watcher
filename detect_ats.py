#!/usr/bin/env python3
"""Auto-detect which applicant-tracking system each company uses.
Reads companies.txt (one per line: "Name" or "Name | careers-page-url"),
probes Greenhouse / Lever / Ashby by likely slugs and scans the careers page
for Workday / ATS links. Writes detected.yaml (read automatically by
job_watcher.py) and unresolved.txt (companies that need a manual source)."""
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job watcher)"}
T = 20


def slug_candidates(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", "and"))
    w = base.split()
    c = ["".join(w), "-".join(w), w[0], "".join(w) + "inc", "".join(w) + "careers"]
    return list(dict.fromkeys(x for x in c if x))


def probe_greenhouse(s):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", headers=HEADERS, timeout=T)
    if r.ok:
        jobs = r.json().get("jobs", [])
        return len(jobs), (jobs[0]["title"] if jobs else "")
    return 0, ""


def probe_lever(s):
    r = requests.get(f"https://api.lever.co/v0/postings/{s}", params={"mode": "json"}, headers=HEADERS, timeout=T)
    if r.ok and isinstance(r.json(), list):
        jobs = r.json()
        return len(jobs), (jobs[0]["text"] if jobs else "")
    return 0, ""


def probe_ashby(s):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{s}", headers=HEADERS, timeout=T)
    if r.ok:
        jobs = r.json().get("jobs", [])
        return len(jobs), (jobs[0]["title"] if jobs else "")
    return 0, ""


def probe_smartrecruiters(s):
    r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings",
                     params={"limit": 1}, headers=HEADERS, timeout=T)
    if r.ok:
        d = r.json()
        return d.get("totalFound", 0), ((d.get("content") or [{}])[0].get("name", ""))
    return 0, ""


def probe_workday(url):
    m = re.match(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", url)
    if not m:
        return 0, ""
    tenant, wd, site = m.groups()
    r = requests.post(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                      json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                      headers={**HEADERS, "Accept": "application/json"}, timeout=T)
    if r.ok and "json" in r.headers.get("content-type", ""):
        d = r.json()
        return d.get("total", 0), ((d.get("jobPostings") or [{}])[0].get("title", ""))
    return 0, ""


def probe_eightfold(val):
    host, domain = val.split("|")
    r = requests.get(f"https://{host}/api/apply/v2/jobs", params={"domain": domain, "start": 0, "num": 1},
                     headers={**HEADERS, "Accept": "application/json"}, timeout=T)
    if r.ok and "json" in r.headers.get("content-type", ""):
        d = r.json()
        pos = d.get("positions") or []
        return (d.get("count") or len(pos)), (pos[0].get("name", "") if pos else "")
    return 0, ""


PROBES = {"eightfold": probe_eightfold, "greenhouse": probe_greenhouse, "lever": probe_lever, "ashby": probe_ashby,
          "smartrecruiters": probe_smartrecruiters}
OVERRIDES = yaml.safe_load((ROOT / "overrides.yaml").read_text(encoding="utf-8")) \
    if (ROOT / "overrides.yaml").exists() else {}
WD_INSTANCES = ["wd1", "wd5", "wd12", "wd3", "wd103", "wd108"]
WD_SITES = ["External", "Careers", "External_Career_Site", "ExternalCareers", "External_Careers",
            "careers", "jobs", "Ext", "{T}", "{T}_Careers", "{T}Careers", "{T}_External"]


def try_candidates(name):
    """Test overrides.yaml candidates; return every one that works."""
    hits = []
    for cand in OVERRIDES.get(name) or []:
        (kind, val), = cand.items()
        # after a hit, only keep looking for extra Workday sites (e.g. Adobe university + experienced)
        if hits and not (kind == "workday" and hits[0][0]["type"] == "workday"):
            continue
        try:
            n, sample = probe_workday(val) if kind == "workday" else PROBES[kind](val)
        except Exception:
            continue
        if n:
            entry = {"name": name, "type": kind}
            if kind == "workday":
                entry.update({"url": val, "search": ["MBA", "intern"]})
            elif kind == "eightfold":
                host, domain = val.split("|")
                entry.update({"host": host, "domain": domain, "search": ["MBA intern", "intern"]})
            else:
                entry["slug"] = val
            hits.append((entry, f"{n} jobs, e.g. {sample}"))
    return hits


def brute_workday(name):
    """Last resort: try common Workday tenant/instance/site patterns."""
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).split()
    tenants = list(dict.fromkeys(["".join(base), base[0]]))
    for t in tenants:
        for wd in WD_INSTANCES:
            for site in WD_SITES:
                url = f"https://{t}.{wd}.myworkdayjobs.com/{site.replace('{T}', t.capitalize())}"
                try:
                    n, sample = probe_workday(url)
                except Exception:
                    continue
                if n:
                    return {"name": name, "type": "workday", "url": url, "search": ["MBA", "intern"]}, \
                        f"{n} jobs, e.g. {sample} (pattern match - double-check)"
    return None, "not found"
PAGE_PATTERNS = [
    ("workday", r"https://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+"),
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([a-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)"),
]


def detect(line):
    name = line.partition("|")[0].strip()
    hits = try_candidates(name)
    if hits:
        return hits
    entry, note = detect_basic(line)
    if entry:
        return [(entry, note)]
    entry, note = brute_workday(name)
    return [(entry, note)] if entry else []


def detect_basic(line):
    name, _, url = (p.strip() for p in line.partition("|"))
    # 1) careers page scan (most reliable when a URL is given)
    if url:
        try:
            page = requests.get(url, headers=HEADERS, timeout=T).text
            for kind, pat in PAGE_PATTERNS:
                m = re.search(pat, page)
                if m and kind == "workday":
                    return {"name": name, "type": "workday", "url": m.group(0),
                            "search": ["MBA", "intern"]}, "found on careers page"
                if m:
                    n, sample = PROBES[kind](m.group(1))
                    if n:
                        return {"name": name, "type": kind, "slug": m.group(1)}, f"{n} jobs, e.g. {sample}"
        except Exception as e:
            print(f"  {name}: page scan failed ({e})")
    # 2) slug guessing; keep the board with the most jobs (old boards go stale)
    best = None
    for s in slug_candidates(name):
        for kind, probe in PROBES.items():
            try:
                n, sample = probe(s)
            except Exception:
                continue
            if n and (best is None or n > best[0]):
                best = (n, {"name": name, "type": kind, "slug": s}, f"{n} jobs, e.g. {sample}")
    return (best[1], best[2]) if best else (None, "not found")


def main():
    lines = [l.strip() for l in (ROOT / "companies.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(detect, lines))
    found, missing = [], []
    for line, hits in zip(lines, results):
        label = line.split("|")[0].strip()
        if not hits:
            missing.append(label)
            print(f"MISS {label}")
        for i, (entry, note) in enumerate(hits):
            if i:  # a 2nd board for the same company (e.g. Adobe university + experienced)
                entry["name"] = f"{entry['name']} ({i + 1})"
            found.append(entry)
            print(f"OK   {entry['name']:30} {entry['type']:15} {entry.get('slug', entry.get('url'))}  ({note})")
    (ROOT / "detected.yaml").write_text(
        "# Auto-generated by detect_ats.py. Check the sample titles in the Actions log:\n"
        "# a guessed slug can occasionally belong to a different company.\n"
        + yaml.safe_dump(found, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (ROOT / "unresolved.txt").write_text("\n".join(missing) + "\n", encoding="utf-8")
    print(f"\n{len(found)} detected, {len(missing)} unresolved")


if __name__ == "__main__":
    main()
