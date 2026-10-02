#!/usr/bin/env python3
"""Audit the "Other internships" list for false negatives.

For every open matching job that is NOT classified MBA-level, this fetches the full
job description, extracts the education / eligibility requirements, and judges whether
an MBA student is eligible:
  1. Rule-based: looks for graduate-level vs undergrad-only wording (always runs, free).
  2. Claude review: if ANTHROPIC_API_KEY has credits, Claude reads the requirements and
     gives a verdict with the deciding quote (skipped automatically if unavailable).
Writes audit.md, with likely false negatives at the top.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import job_watcher as jw

GRAD = re.compile(
    r"master[’'`]?s?\b|graduate\s+(?:degree|program|student|school|level)|advanced\s+degree|"
    r"post-?graduate|\bJ\.?D\b|\bMS\b|\bM\.S\b|\bMSc\b|\bMPP\b|\bMPA\b|\bMFin\b|"
    r"first[- ]year\s+(?:graduate|master)|second[- ]year|business\s+school", re.I)
UNDERGRAD = re.compile(
    r"bachelor[’'`]?s?|undergraduate|\bjunior\b|\bsophomore\b|rising\s+senior|"
    r"\bfreshman\b|associate[’'`]?s\s+degree|high\s+school", re.I)
EDU_HINT = re.compile(
    r"degree|bachelor|master|MBA|graduate|undergraduate|pursuing|enrolled|student|class\s+of|"
    r"graduation|junior|senior\s+year|sophomore|PhD|university|college", re.I)


def requirement_snippets(text, limit=4):
    parts = re.split(r"(?<=[.!?])\s+|\s+[•·▪]\s+|\s{2,}", text or "")
    hits = [p.strip() for p in parts if EDU_HINT.search(p) and 15 < len(p.strip()) < 400]
    return list(dict.fromkeys(hits))[:limit]


def rule_verdict(job):
    text = job.get("description", "")
    if not text:
        return "unclear", "No description could be fetched."
    snips = requirement_snippets(text)
    req = " ".join(snips)
    if jw.mba_in_description(job):
        return "eligible", "Mentions an MBA (should already be classified; check the cache)."
    g, u = bool(GRAD.search(req)), bool(UNDERGRAD.search(req))
    if g:
        return "possible", "Graduate-level wording in the requirements."
    if u:
        return "undergrad", "Requirements mention undergraduate study only."
    return "unclear", "No education requirement found."


LLM_OK = bool(os.environ.get("ANTHROPIC_API_KEY"))


def llm_verdict(job):
    global LLM_OK
    if not LLM_OK:
        return None
    req = "\n".join(requirement_snippets(job.get("description", ""), limit=8)) or job.get("description", "")[:4000]
    prompt = (
        "You audit internship postings for an MBA student (currently in a full-time MBA program, "
        "graduating 2028, seeking a Summer 2027 internship).\n"
        f"Title: {job['title']}\nCompany: {job['company']}\n"
        f"Eligibility / education text from the job description:\n<req>\n{req[:4000]}\n</req>\n"
        "Can an MBA student apply? Answer 'yes' if MBA/graduate/master's students are explicitly eligible "
        "or the role is clearly graduate-level; 'no' if it is restricted to undergraduates (or another "
        "specific degree that excludes MBAs); 'unclear' otherwise.\n"
        'Reply ONLY with JSON: {"verdict": "yes|no|unclear", "quote": "<the deciding sentence, max 30 words>"}')
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                   "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": jw.CONFIG.get("model", "claude-haiku-4-5-20251001"),
                                "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]})
        no_credit = r.status_code in (400, 402, 429) and "credit" in r.text.lower()
        if no_credit or r.status_code in (401, 403):
            print(f"Claude review unavailable ({r.status_code}); continuing with rules only.")
            LLM_OK = False
            return None
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json()["content"])
        return json.loads(re.search(r"\{.*\}", out, re.S).group(0))
    except Exception as e:
        print(f"  Claude review failed for {job['title']}: {e}")
        return None


def main():
    seen = json.loads(jw.SEEN_FILE.read_text()) if jw.SEEN_FILE.exists() else {}
    mba_cache = seen.get("_mba", {})
    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(jw.fetch, jw.all_companies()))

    others = []
    for c, jobs, err in results:
        for j in jobs:
            if not (jw.title_matches(j["title"]) and jw.location_ok(j["location"])):
                continue
            if jw.mba_in_title(j["title"]) or mba_cache.get(c["name"], {}).get(j["id"]):
                continue
            j["company"] = c["name"]
            others.append(j)
    print(f"Auditing {len(others)} 'Other internships' postings...")

    with ThreadPoolExecutor(10) as ex:
        list(ex.map(jw.fill_description, others))

    rows = {"flag": [], "unclear": [], "undergrad": []}
    for j in others:
        rv, why = rule_verdict(j)
        lv = llm_verdict(j) if rv != "eligible" else None
        quote = (lv or {}).get("quote") or " … ".join(requirement_snippets(j.get("description", ""), 2))
        if rv == "eligible" or (lv and lv.get("verdict") == "yes") or (not lv and rv == "possible"):
            bucket = "flag"
        elif (lv and lv.get("verdict") == "no") or (not lv and rv == "undergrad"):
            bucket = "undergrad"
        else:
            bucket = "unclear"
        src = f"Claude: {lv['verdict']}" if lv else f"Rules: {why}"
        rows[bucket].append((j, src, quote))

    def table(items):
        out = ["| Company | Role | Why | Requirement text |", "|---|---|---|---|"]
        for j, src, q in sorted(items, key=lambda x: (x[0]["company"].lower(), x[0]["title"].lower())):
            q = (q or "—").replace("|", "/").replace("\n", " ")[:300]
            out.append(f"| {j['company']} | [{j['title'].replace('|', '/')}]({j['url']}) | {src} | {q} |")
        return out

    md = [f"# Audit of 'Other internships' ({len(others)} postings)", "",
          f"Run {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. "
          + ("Rules + Claude review." if LLM_OK else "Rules only (Claude review unavailable: add API credits for a deeper check)."),
          "", f"## Likely false negatives: MBA students possibly eligible ({len(rows['flag'])})", "",
          *table(rows["flag"]), "",
          f"## Unclear: worth a quick look ({len(rows['unclear'])})", "", *table(rows["unclear"]), "",
          f"## Undergrad only ({len(rows['undergrad'])})", "", *table(rows["undergrad"])]
    (jw.ROOT / "audit.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nAUDIT: {len(rows['flag'])} likely false negatives, {len(rows['unclear'])} unclear, "
          f"{len(rows['undergrad'])} undergrad-only")


if __name__ == "__main__":
    main()
