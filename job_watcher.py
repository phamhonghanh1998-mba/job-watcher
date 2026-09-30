#!/usr/bin/env python3
"""Job watcher: polls company career sites for new postings that match your
keywords, scores fit against your resume with Claude, and pushes a phone
alert via ntfy.sh. State is kept in seen.json so you only hear about NEW jobs."""
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
SEEN_FILE = ROOT / "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job watcher)"}
TIMEOUT = 30


def text_of(raw_html):
    return BeautifulSoup(raw_html or "", "html.parser").get_text(" ", strip=True)


# ---------------------------------------------------------------- sources
# Each returns a list of {id, title, location, url, description}

def src_greenhouse(c):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs",
                     params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j["id"]), "title": j["title"],
             "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"],
             "description": text_of(html.unescape(j.get("content", "")))}
            for j in r.json().get("jobs", [])]


def src_lever(c):
    r = requests.get(f"https://api.lever.co/v0/postings/{c['slug']}",
                     params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": j["id"], "title": j["text"],
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j["hostedUrl"],
             "description": j.get("descriptionPlain", "") + " " +
             " ".join(text_of(l.get("content", "")) for l in j.get("lists", []))}
            for j in r.json()]


def src_ashby(c):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": j["id"], "title": j["title"], "location": j.get("location", ""),
             "url": j["jobUrl"], "description": j.get("descriptionPlain", "")}
            for j in r.json().get("jobs", [])]


def src_workday(c):
    # c["url"] like https://tenant.wd5.myworkdayjobs.com/SiteName
    u = urlparse(c["url"])
    host, tenant = u.netloc, u.netloc.split(".")[0]
    site = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)][0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    jobs = []
    for q in c.get("search", [""]):
        r = requests.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20,
                                               "offset": 0, "searchText": q},
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobPostings", []):
            jobs.append({"id": j["externalPath"], "title": j["title"],
                         "location": j.get("locationsText", ""),
                         "url": f"https://{host}/en-US/{site}{j['externalPath']}",
                         "description": "", "_detail": f"{api}{j['externalPath']}"})
    return list({j["id"]: j for j in jobs}.values())


def src_watch(c):
    # Generic: scan a (server-rendered) careers page for links whose text matches keywords
    r = requests.get(c["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = {}
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        if 5 <= len(title) <= 200 and title_matches(title):
            link = urljoin(c["url"], a["href"])
            jobs[link] = {"id": link, "title": title, "location": "",
                          "url": link, "description": ""}
    return list(jobs.values())


def src_amazon(c):
    jobs = {}
    for q in c.get("search", ["MBA intern"]):
        r = requests.get("https://www.amazon.jobs/en/search.json", headers=HEADERS, timeout=TIMEOUT,
                         params={"base_query": q, "result_limit": 100, "sort": "recent"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            path = j.get("job_path", "")
            jid = str(j.get("id_icims") or j.get("id") or path)
            jobs[jid] = {"id": jid, "title": j.get("title", ""),
                         "location": j.get("normalized_location") or j.get("location", ""),
                         "url": "https://www.amazon.jobs" + path,
                         "description": text_of(" ".join(j.get(k) or "" for k in (
                             "description", "basic_qualifications", "preferred_qualifications")))}
    return list(jobs.values())


def src_smartrecruiters(c):
    jobs = {}
    base = f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings"
    for q in c.get("search", ["intern", "MBA"]):
        r = requests.get(base, params={"q": q, "limit": 100}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("content", []):
            loc = j.get("location") or {}
            jobs[j["id"]] = {"id": j["id"], "title": j.get("name", ""),
                             "location": ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                               loc.get("country")) if x),
                             "url": f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                             "description": "", "_sr": f"{base}/{j['id']}"}
    return list(jobs.values())


SOURCES = {"amazon": src_amazon, "smartrecruiters": src_smartrecruiters, "greenhouse": src_greenhouse, "lever": src_lever, "ashby": src_ashby,
           "workday": src_workday, "watch": src_watch}


# ---------------------------------------------------------------- filters

def has_any(text, terms):
    return any(re.search(r"\b" + re.escape(t) + r"\b", text, re.I) for t in terms)


def title_matches(title):
    k = CONFIG["keywords"]
    if not has_any(title, k["include"]) or has_any(title, k.get("exclude", [])):
        return False
    roles = k.get("roles", [])
    if not roles or has_any(title, roles):
        return True
    # Generic titles like "MBA Summer Intern 2027" name no function: let fit scoring decide
    return has_any(title, k.get("generic_ok", [])) and not has_any(title, k.get("other_functions", []))


def location_ok(loc):
    wanted = CONFIG["keywords"].get("locations", [])
    return not wanted or not loc or has_any(loc, wanted)


def fill_description(job):
    if job["description"]:
        return
    try:
        if "_sr" in job:  # SmartRecruiters detail API
            sections = (requests.get(job["_sr"], headers=HEADERS, timeout=TIMEOUT)
                        .json().get("jobAd", {}).get("sections", {}))
            job["description"] = text_of(" ".join(v.get("text", "") for v in sections.values()
                                                   if isinstance(v, dict)))
        elif "_detail" in job:  # Workday detail API
            r = requests.get(job["_detail"], headers=HEADERS, timeout=TIMEOUT)
            job["description"] = text_of(r.json()["jobPostingInfo"]["jobDescription"])
        else:
            r = requests.get(job["url"], headers=HEADERS, timeout=TIMEOUT)
            job["description"] = text_of(r.text)[:15000]
    except Exception as e:
        print(f"  could not fetch description: {e}")


# ---------------------------------------------------------------- fit scoring

def load_resume():
    if os.environ.get("RESUME_TEXT"):
        return os.environ["RESUME_TEXT"]
    p = ROOT / "resume.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def score_fit(job, resume):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not resume:
        return None
    prompt = (
        "Screen this job posting for the candidate below. Be strict and honest.\n"
        f"<resume>\n{resume}\n</resume>\n"
        f"<job title=\"{job['title']}\" company=\"{job['company']}\" "
        f"location=\"{job['location']}\">\n{job['description'][:12000]}\n</job>\n"
        'Reply with ONLY JSON: {"score": <0-100 fit>, "reason": "<one sentence>", '
        '"gaps": "<missing hard requirements, or none>"}')
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=90,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": CONFIG.get("model", "claude-haiku-4-5-20251001"),
                                "max_tokens": 300,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json()["content"])
        return json.loads(re.search(r"\{.*\}", out, re.S).group(0))
    except Exception as e:
        print(f"  scoring failed: {e}")
        return None


# ---------------------------------------------------------------- alerts

def notify(job, fit):
    topic = os.environ.get("NTFY_TOPIC") or CONFIG.get("ntfy_topic")
    header = f"{job['company']}: {job['title']}"
    body = job["location"] or ""
    priority = 3
    if fit:
        body = f"Fit {fit.get('score')}/100 · {fit.get('reason', '')}\nGaps: {fit.get('gaps', '')}\n{body}"
        priority = 5 if fit.get("score", 0) >= CONFIG.get("high_fit_score", 75) else 3
    print(f"  NEW -> {header}\n     {body}\n     {job['url']}")
    if topic:
        requests.post("https://ntfy.sh/", timeout=TIMEOUT,
                      json={"topic": topic, "title": header[:250], "message": body,
                            "click": job["url"], "priority": priority, "tags": ["briefcase"]})


# ---------------------------------------------------------------- main

def all_companies():
    cs = list(CONFIG.get("companies") or [])
    det = ROOT / "detected.yaml"
    if det.exists():  # output of detect_ats.py; entries in config.yaml take priority
        names = {c["name"].lower() for c in cs}
        cs += [c for c in (yaml.safe_load(det.read_text(encoding="utf-8")) or [])
               if c["name"].lower() not in names]
    return cs


def fetch(c):
    try:
        return c, SOURCES[c["type"]](c), None
    except Exception as e:
        return c, [], e


def main():
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    resume = load_resume()
    min_score = CONFIG.get("min_fit_score", 0)

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(fetch, all_companies()))

    for c, jobs, err in results:
        name = c["name"]
        print(f"[{name}] ({c['type']})")
        if err:
            print(f"  ERROR: {err}")
            continue

        first_run = name not in seen
        known = set(seen.get(name, []))
        matches = [j for j in jobs if title_matches(j["title"]) and location_ok(j["location"])]
        new = [j for j in matches if j["id"] not in known]
        print(f"  {len(jobs)} jobs, {len(matches)} match keywords, {len(new)} new")

        if not first_run:  # first run only records what exists, so you aren't flooded
            for j in new:
                j["company"] = name
                fill_description(j)
                fit = score_fit(j, resume)
                if fit is None or fit.get("score", 0) >= min_score:
                    notify(j, fit)
                else:
                    print(f"  skipped (fit {fit.get('score')}): {j['title']}")

        seen[name] = sorted(known | {j["id"] for j in jobs})

    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
