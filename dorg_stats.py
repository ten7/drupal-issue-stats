#!/usr/bin/env python3
# drupal-issue-stats — Drupal.org issue-queue analyzer
# Copyright (C) 2026 TEN7 LLC
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
# Public License (the bundled LICENSE file) for more details.
"""
dorg_stats.py — Drupal.org issue-queue analyzer.

Give it a drupal.org project (URL or machine name), and it walks the entire
issue queue (open + closed) via the public api-d7 REST API, computes a battery
of statistics, and writes a self-contained HTML report you can print to PDF.

Usage:
    python3 dorg_stats.py https://www.drupal.org/project/lms
    python3 dorg_stats.py lms --deep --tone --open report.html

Only third-party dependency: requests  (pip install requests)
Optional: weasyprint, for direct --pdf output.
"""

from __future__ import annotations

import argparse
import datetime as dt
from datetime import timezone
import json
import math
import os
import re
import statistics
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from html import escape
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("This tool needs the 'requests' package. Install it with:\n"
             "    pip install requests")

__version__ = "1.0.0"

API = "https://www.drupal.org/api-d7"
NOW = int(time.time())
DAY = 86400

# ---------------------------------------------------------------------------
# Drupal.org field code maps (canonical core values).
# ---------------------------------------------------------------------------
STATUS = {
    1: "Active", 2: "Fixed", 3: "Closed (duplicate)", 4: "Postponed",
    5: "Closed (won't fix)", 6: "Closed (cannot reproduce)",
    7: "Closed (fixed)", 8: "Needs review", 13: "Needs work",
    14: "Reviewed & tested by the community", 15: "Patch (to be ported)",
    16: "Postponed (maintainer needs more info)", 17: "Closed (outdated)",
    18: "Closed (works as designed)",
}
# Statuses that count as "resolved / closed" for open-vs-closed math.
CLOSED_STATUSES = {2, 3, 5, 6, 7, 17, 18}

PRIORITY = {400: "Critical", 300: "Major", 200: "Normal", 100: "Minor"}
CATEGORY = {1: "Bug report", 2: "Task", 3: "Feature request",
            4: "Support request", 5: "Plan"}


# ---------------------------------------------------------------------------
# HTTP layer: polite, retrying, cached.
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, cache_dir, delay=0.34, no_cache=False, verbose=False):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "dorg-stats/1.0 (+local issue-queue analyzer)")
        self.delay = delay
        self.cache_dir = cache_dir
        self.no_cache = no_cache
        self.verbose = verbose
        self._last = 0.0
        if not no_cache:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, url):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", url)[:180]
        return os.path.join(self.cache_dir, safe + ".json")

    def get(self, url, params=None):
        full = url
        if params:
            from urllib.parse import urlencode
            full = url + "?" + urlencode(params)
        cp = self._cache_path(full)
        if not self.no_cache and os.path.exists(cp):
            with open(cp) as f:
                return json.load(f)

        # Throttle.
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)

        for attempt in range(6):
            try:
                r = self.s.get(full, timeout=30)
            except requests.RequestException as e:
                wait = 2 ** attempt
                if self.verbose:
                    print(f"  ! network error ({e}); retrying in {wait}s",
                          file=sys.stderr)
                time.sleep(wait)
                continue
            self._last = time.time()
            if r.status_code == 200:
                data = r.json()
                if not self.no_cache:
                    with open(cp, "w") as f:
                        json.dump(data, f)
                return data
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                if self.verbose:
                    print(f"  ! HTTP {r.status_code}; retrying in {wait}s",
                          file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {r.status_code} for {full}")
        raise RuntimeError(f"Gave up after retries: {full}")


# ---------------------------------------------------------------------------
# Project + issue fetching.
# ---------------------------------------------------------------------------
def machine_name_from(arg):
    """Accept a full URL or a bare machine name and return the machine name."""
    arg = arg.strip()
    if "drupal.org" not in arg and "/" not in arg:
        return arg
    path = urlparse(arg).path
    # .../project/issues/<name>  or  .../project/<name>
    m = re.search(r"/project/(?:issues/)?([a-z0-9_]+)", path)
    if m:
        return m.group(1)
    return path.rstrip("/").split("/")[-1]


def get_project(client, machine):
    """Return the project node for a machine name, trying a few node types."""
    for ptype in ("project_module", "project_theme", "project_distribution",
                  "project_core", None):
        params = {"field_project_machine_name": machine}
        if ptype:
            params["type"] = ptype
        data = client.get(f"{API}/node.json", params)
        lst = data.get("list") or []
        if lst:
            return lst[0]
    raise SystemExit(
        f"Could not find a project with machine name '{machine}'. "
        f"Check the URL (e.g. https://www.drupal.org/project/lms).")


def iter_issues(client, project_nid, verbose=False):
    """Yield every project_issue node for a project.

    Note: the api-d7 'next' link comes back without the '.json' extension
    (e.g. /api-d7/node?page=1), which 404s. So we paginate manually against
    node.json with an explicit page param and stop at the API's own 'last'
    page (falling back to: stop when a page returns no items).
    """
    page = 0
    last_page = None
    while page < 100000:  # safety cap
        params = {"type": "project_issue",
                  "field_project": project_nid, "page": page}
        data = client.get(f"{API}/node.json", params)
        items = data.get("list") or []
        for node in items:
            yield node
        if verbose:
            print(f"  …page {page}: {len(items):,} issues", file=sys.stderr)

        if last_page is None:
            m = re.search(r"[?&]page=(\d+)", str(data.get("last") or ""))
            if m:
                last_page = int(m.group(1))
        if last_page is not None:
            if page >= last_page:
                break
        elif not items:
            break
        page += 1


# ---------------------------------------------------------------------------
# Small helpers for messy API values.
# ---------------------------------------------------------------------------
def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def author_id(node):
    a = node.get("author")
    if isinstance(a, dict):
        return as_int(a.get("id"))
    return None


def author_name_inline(node):
    a = node.get("author")
    if isinstance(a, dict) and a.get("name"):
        return a["name"]
    return None


def resolve_username(client, uid, cache):
    if uid in cache:
        return cache[uid]
    name = f"uid:{uid}"
    try:
        data = client.get(f"{API}/user/{uid}.json")
        if isinstance(data, dict) and data.get("name"):
            name = data["name"]
    except Exception:
        pass
    cache[uid] = name
    return name


def core_branch(version):
    """'8.x-1.x-dev' -> '8.x'; '4.0.x-dev' (semver) -> '4.0.x' family hint."""
    if not version:
        return "unspecified"
    m = re.match(r"(\d+\.x)", version)
    if m:
        return m.group(1)
    m = re.match(r"(\d+)\.", version)
    if m:
        return f"{m.group(1)}.x (semver)"
    return version


def human_days(days):
    if days is None:
        return "—"
    if days < 1:
        return "<1 day"
    if days < 60:
        return f"{days:.0f} days"
    if days < 730:
        return f"{days/30.44:.1f} months"
    return f"{days/365.25:.1f} years"


def human_days_short(days):
    if days is None:
        return "—"
    if days < 60:
        return f"{days:.0f}d"
    if days < 730:
        return f"{days/30.44:.0f}mo"
    return f"{days/365.25:.1f}yr"


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Core-branch support posture (heuristic; see methodology note in report).
CURRENT_BRANCHES = {"10.x", "11.x"}
LEGACY_BRANCHES = {"5.x", "6.x", "7.x", "8.x", "9.x"}


def gini(values):
    """Gini coefficient over positive counts. 0 = perfectly even, →1 = concentrated."""
    vals = sorted(v for v in values if v and v > 0)
    n = len(vals)
    total = sum(vals)
    if n == 0 or total == 0:
        return None
    idx_sum = sum((i + 1) * v for i, v in enumerate(vals))
    return (2 * idx_sum) / (n * total) - (n + 1) / n


def top_share(counter, k):
    total = sum(counter.values())
    if not total:
        return None
    return sum(v for _, v in counter.most_common(k)) / total


def branch_posture(branch):
    if branch in CURRENT_BRANCHES:
        return "current"
    if branch in LEGACY_BRANCHES:
        return "legacy / EOL"
    if "semver" in branch:
        return "current (semver)"
    return "unspecified"


def extract_tags(node):
    """field_issue_tags can be a list of term refs, strings, or a dict. Be lax."""
    raw = node.get("field_issue_tags") or node.get("taxonomy_vocabulary_9")
    out = []
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for t in raw:
            if isinstance(t, dict):
                out.append(("id", as_int(t.get("id")), t.get("name")))
            elif isinstance(t, str) and t.strip():
                out.append(("name", None, t.strip()))
    elif isinstance(raw, str) and raw.strip():
        for piece in re.split(r"[,]", raw):
            piece = piece.strip()
            if piece:
                out.append(("name", None, piece))
    return out


def resolve_tag_name(client, tid, cache):
    if tid in cache:
        return cache[tid]
    name = f"tag:{tid}"
    try:
        data = client.get(f"{API}/taxonomy_term/{tid}.json")
        if isinstance(data, dict) and data.get("name"):
            name = data["name"]
    except Exception:
        pass
    cache[tid] = name
    return name


# ---------------------------------------------------------------------------
# Core analysis.
# ---------------------------------------------------------------------------
def analyze(issues):
    s = {}
    n = len(issues)
    s["total"] = n
    if n == 0:
        return s

    status_c, prio_c, cat_c, branch_c, ver_c, comp_c = (
        Counter(), Counter(), Counter(), Counter(), Counter(), Counter())
    author_c = Counter()
    author_names = {}
    created_year, closed_year = Counter(), Counter()
    open_ages, resolution_times = [], []
    comment_total = 0
    comment_buckets = Counter()
    most_commented = []
    open_count = 0
    recent30 = recent90 = stale365 = 0
    has_files = 0
    cohort_res = defaultdict(list)        # opened-year -> [resolution days]
    needs_review_ages, rtbc_ages = [], []  # ages of currently-open NR / RTBC
    open_branch_posture = Counter()
    month_c, weekday_c = Counter(), Counter()
    tag_c = Counter()
    tag_names = {}

    for it in issues:
        st = as_int(it.get("field_issue_status"))
        status_c[STATUS.get(st, f"Unknown ({st})")] += 1
        prio_c[PRIORITY.get(as_int(it.get("field_issue_priority")),
                            "Unspecified")] += 1
        cat_c[CATEGORY.get(as_int(it.get("field_issue_category")),
                           "Unspecified")] += 1
        ver = it.get("field_issue_version") or ""
        ver_c[ver or "unspecified"] += 1
        branch_c[core_branch(ver)] += 1
        comp = it.get("field_issue_component") or "unspecified"
        comp_c[comp] += 1

        aid = author_id(it)
        if aid is not None:
            author_c[aid] += 1
            nm = author_name_inline(it)
            if nm:
                author_names[aid] = nm

        created = as_int(it.get("created"))
        changed = as_int(it.get("changed")) or created
        if created:
            cdt = dt.datetime.fromtimestamp(created, tz=timezone.utc)
            created_year[cdt.year] += 1
            month_c[cdt.month] += 1
            weekday_c[cdt.weekday()] += 1
        is_closed = st in CLOSED_STATUSES
        if is_closed:
            if changed:
                closed_year[dt.datetime.fromtimestamp(changed, tz=timezone.utc).year] += 1
            if created and changed and changed >= created:
                days = (changed - created) / DAY
                resolution_times.append(days)
                cohort_res[cdt.year].append(days)
        else:
            open_count += 1
            if created:
                open_ages.append((NOW - created) / DAY)
            open_branch_posture[branch_posture(core_branch(ver))] += 1
            nr_age = (NOW - changed) / DAY if changed else None
            if st == 8 and nr_age is not None:        # Needs review
                needs_review_ages.append(nr_age)
            elif st == 14 and nr_age is not None:      # RTBC
                rtbc_ages.append(nr_age)

        for kind, tid, name in extract_tags(it):
            key = ("id", tid) if kind == "id" and tid else ("name", name)
            tag_c[key] += 1
            if name:
                tag_names[key] = name

        cc = as_int(it.get("comment_count")) or 0
        comment_total += cc
        comment_buckets[bucket_comments(cc)] += 1
        most_commented.append((cc, it.get("title", ""),
                               issue_url(it), st))

        if changed:
            age = (NOW - changed) / DAY
            if age <= 30:
                recent30 += 1
            if age <= 90:
                recent90 += 1
            if not is_closed and age > 365:
                stale365 += 1

        files = it.get("field_issue_files")
        if isinstance(files, list) and files:
            has_files += 1

    s["status"] = status_c
    s["priority"] = prio_c
    s["category"] = cat_c
    s["branch"] = branch_c
    s["version"] = ver_c
    s["component"] = comp_c
    s["author_counts"] = author_c
    s["author_names"] = author_names
    s["created_year"] = dict(sorted(created_year.items()))
    s["closed_year"] = dict(sorted(closed_year.items()))
    s["open_count"] = open_count
    s["closed_count"] = n - open_count
    s["comment_total"] = comment_total
    s["comment_avg"] = comment_total / n
    s["comment_buckets"] = comment_buckets
    s["recent30"] = recent30
    s["recent90"] = recent90
    s["stale365"] = stale365
    s["issues_with_files"] = has_files

    most_commented.sort(key=lambda x: x[0], reverse=True)
    s["top_commented"] = most_commented[:15]

    if resolution_times:
        s["res_median"] = statistics.median(resolution_times)
        s["res_mean"] = statistics.mean(resolution_times)
        s["res_max"] = max(resolution_times)
        s["res_count"] = len(resolution_times)
    if open_ages:
        s["open_median_age"] = statistics.median(open_ages)
        s["open_mean_age"] = statistics.mean(open_ages)
        s["open_max_age"] = max(open_ages)

    # --- Resolution-time trend by opened-year cohort (>=4 issues/year) ---
    s["cohort_res"] = {
        yr: (statistics.median(v), len(v))
        for yr, v in sorted(cohort_res.items()) if len(v) >= 4}

    # --- Needs-review / RTBC backlog age ---
    def _backlog(ages):
        if not ages:
            return None
        return {"count": len(ages), "median": statistics.median(ages),
                "mean": statistics.mean(ages), "oldest": max(ages)}
    s["needs_review"] = _backlog(needs_review_ages)
    s["rtbc"] = _backlog(rtbc_ages)

    # --- Branch / version health (open issues) ---
    s["open_branch_posture"] = open_branch_posture

    # --- Seasonality ---
    s["by_month"] = month_c
    s["by_weekday"] = weekday_c

    # --- Tags ---
    s["tags"] = tag_c
    s["tag_names"] = tag_names

    # --- Contribution concentration (bus factor) on issue authors ---
    s["author_gini"] = gini(author_c.values())
    s["author_top1"] = top_share(author_c, 1)
    s["author_top3"] = top_share(author_c, 3)
    s["author_top10"] = top_share(author_c, 10)
    s["author_unique"] = len([v for v in author_c.values() if v])

    # Oldest still-open issues.
    oldest = sorted(
        (it for it in issues
         if as_int(it.get("field_issue_status")) not in CLOSED_STATUSES),
        key=lambda it: as_int(it.get("created")) or NOW)[:15]
    s["oldest_open"] = [
        ((NOW - (as_int(it.get("created")) or NOW)) / DAY,
         it.get("title", ""), issue_url(it),
         STATUS.get(as_int(it.get("field_issue_status")), "?"))
        for it in oldest]
    return s


def bucket_comments(cc):
    if cc == 0:
        return "0"
    if cc <= 5:
        return "1–5"
    if cc <= 10:
        return "6–10"
    if cc <= 25:
        return "11–25"
    if cc <= 50:
        return "26–50"
    return "50+"


def issue_url(node):
    u = node.get("url")
    if u:
        return u
    nid = node.get("nid")
    return f"https://www.drupal.org/node/{nid}" if nid else "#"


# ---------------------------------------------------------------------------
# Deep mode: comment-level analysis (slower).
# ---------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s):
    if not s:
        return ""
    return WS_RE.sub(" ", TAG_RE.sub(" ", s)).strip()


# Markers that a merge request (rather than an uploaded patch) is in play.
MR_RE = re.compile(
    r"(merge request|/-/merge_requests/|git\.drupalcode\.org|issue fork|"
    r"created an issue fork|opened merge request|!\d{2,})", re.I)
PATCH_RE = re.compile(r"\.(patch|diff)\b", re.I)

# Coarse contention lexicon for the civility *heuristic* (NOT real sentiment).
# Intentionally conservative; this only flags threads for human review.
CONTENTION_TERMS = [
    "ridiculous", "unacceptable", "nonsense", "absurd", "frustrated",
    "frustrating", "annoying", "annoyed", "rude", "hostile", "toxic",
    "disrespect", "insulting", "insult", "stupid", "idiot", "incompetent",
    "useless", "pathetic", "garbage", "broken promise", "ignored", "ignoring",
    "stop wasting", "waste of time", "are you serious", "obviously",
    "clearly you", "read the", "as i already said", "for the last time",
    "wtf", "wth", "shut up", "go away",
]
CONTENTION_RE = re.compile(
    "|".join(re.escape(t) for t in CONTENTION_TERMS), re.I)
CAPS_RE = re.compile(r"\b[A-Z]{4,}\b")


def contention_score(text):
    """Coarse 0+ score; higher = more likely a heated comment. Heuristic only."""
    if not text:
        return 0
    score = len(CONTENTION_RE.findall(text))
    score += min(3, len(re.findall(r"!{2,}", text)))      # !! / !!!
    score += min(3, len(CAPS_RE.findall(text)) // 2)      # shouting in caps
    return score


# ---------------------------------------------------------------------------
# Module-level tone profile (multi-dimensional, aggregate only).
#
# Sentiment uses VADER (Hutto & Gilbert, 2014) when the optional vaderSentiment
# package is installed, else a small built-in lexicon. Category rates below are
# lexicon heuristics. This is a coarse, English-only, sarcasm-blind signal for
# community-health framing (cf. CHAOSS) — never a judgment of individuals.
# ---------------------------------------------------------------------------
GRATITUDE_TERMS = [
    "thank you", "thanks", "thx", "appreciate", "appreciated", "grateful",
    "great work", "nice work", "good work", "well done", "awesome", "kudos",
    "much appreciated", "cheers", "you rock", "lifesaver", "fantastic",
    "amazing", "brilliant", "love this", "great catch", "good catch",
]
POLITENESS_TERMS = [
    "please", "could you", "would you", "if you don't mind", "sorry",
    "apologies", "my apologies", "would it be possible", "kindly",
    "just wondering", "no worries", "if possible", "when you get a chance",
    "thanks in advance", "happy to", "feel free",
]
ACCUSATION_TERMS = [
    "you didn't", "you did not", "you should have", "you need to",
    "you failed", "you clearly", "as i said", "as i already", "like i said",
    "read the issue", "read the docs", "obviously", "clearly you",
    "for the last time", "how many times", "again and again",
]
PROFANITY_TERMS = [
    "wtf", "stfu", "damn", "crap", "bullshit", "bs", "hell no", "screw this",
]
GRATITUDE_RE = re.compile("|".join(re.escape(t) for t in GRATITUDE_TERMS), re.I)
POLITENESS_RE = re.compile("|".join(re.escape(t) for t in POLITENESS_TERMS), re.I)
ACCUSATION_RE = re.compile("|".join(re.escape(t) for t in ACCUSATION_TERMS), re.I)
PROFANITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in PROFANITY_TERMS) + r")\b", re.I)

# Tiny fallback sentiment lexicon (only used when VADER isn't installed).
POS_RE = re.compile(
    r"\b(thanks|thank|great|good|nice|awesome|perfect|works|fixed|love|"
    r"appreciate|excellent|helpful|happy|cool|agree|yes)\b", re.I)
NEG_RE = re.compile(
    r"\b(broken|fail|failed|error|bug|wrong|bad|hate|annoying|frustrat\w*|"
    r"useless|ridiculous|terrible|awful|no|not working|doesn't work|"
    r"can't|cannot|stuck|confusing)\b", re.I)

# Automated / non-human comment authors to exclude from tone scoring.
BOT_NAMES = {"System Message", "Project update bot", "project update bot",
             "System", "Drupal.org webmasters"}


class ToneScorer:
    """Sentiment via VADER if available, else a built-in lexicon fallback."""

    def __init__(self):
        self.vader = None
        self.engine = "built-in lexicon"
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self.vader = SentimentIntensityAnalyzer()
            self.engine = "VADER (Hutto & Gilbert 2014)"
        except Exception:
            pass

    def compound(self, text):
        if not text:
            return 0.0
        if self.vader is not None:
            try:
                return self.vader.polarity_scores(text)["compound"]
            except Exception:
                return 0.0
        pos = len(POS_RE.findall(text))
        neg = len(NEG_RE.findall(text))
        tot = pos + neg
        return (pos - neg) / tot if tot else 0.0


_TONE_SCORER = None


def get_tone_scorer():
    global _TONE_SCORER
    if _TONE_SCORER is None:
        _TONE_SCORER = ToneScorer()
    return _TONE_SCORER


def categorize_tone(text):
    """Boolean category hits for one comment (presence, not count)."""
    return {
        "grateful": bool(GRATITUDE_RE.search(text)),
        "polite": bool(POLITENESS_RE.search(text)),
        "accusatory": bool(ACCUSATION_RE.search(text)),
        "profane": bool(PROFANITY_RE.search(text)),
        "contentious": contention_score(text) >= 2,
    }


def civility_band(index):
    if index >= 78:
        return "warm & appreciative"
    if index >= 62:
        return "civil & constructive"
    if index >= 48:
        return "mixed"
    return "frequently tense"


def parse_maintainer_ids(project, override):
    """Best-effort maintainer uid set from the project node, plus any override."""
    ids = set()
    if override:
        for tok in re.split(r"[,\s]+", override):
            n = as_int(tok)
            if n is not None:
                ids.add(n)
    for key in ("field_maintainers", "co_maintainers", "maintainers",
                "field_co_maintainers"):
        v = project.get(key)
        items = v if isinstance(v, list) else ([v] if v else [])
        for it in items:
            if isinstance(it, dict):
                n = as_int(it.get("id"))
                if n is not None:
                    ids.add(n)
    return ids


def comment_status(c):
    """Extract the issue-status snapshot a comment carries, if any."""
    for k in ("field_issue_status", "new_status", "status"):
        if k in c:
            n = as_int(c.get(k))
            if n is not None:
                return n
    return None


def comment_author_id(c):
    a = c.get("author")
    return as_int(a.get("id")) if isinstance(a, dict) else None


def comment_body_text(c):
    body = c.get("comment_body") or c.get("body") or ""
    if isinstance(body, dict):
        body = body.get("value") or body.get("safe_value") or ""
    return strip_html(body)


def deep_comments(client, issues, cap, verbose=False, maintainers=None,
                  tone=False):
    """Fetch comments for up to `cap` issues and reconstruct per-issue
    timelines, deriving: commenters, first-response, reopen rate, time-in-
    status, review-state responsiveness, patch-vs-MR mix, and (when tone=True)
    a civility heuristic. All timeline-derived metrics depend on comments
    carrying a status snapshot / body; where they don't, the metric degrades
    to empty."""
    maintainers = maintainers or set()
    ordered = sorted(issues,
                     key=lambda it: as_int(it.get("comment_count")) or 0,
                     reverse=True)[:cap]
    commenter_c = Counter()
    first_response = []
    reopen_issues = reopen_observable = 0
    dwell = defaultdict(list)            # status code -> [days]
    review_response = []                 # days from NR/RTBC to next reply
    maint_first_reply = []               # days created -> first maintainer reply
    patch_year, mr_year, total_year = Counter(), Counter(), Counter()
    comments_scanned = heated_comments = 0
    issue_heat = []                      # (score, title, url)
    status_snapshots_seen = bodies_seen = 0
    commenter_names = {}
    tone_scorer = get_tone_scorer() if tone else None
    tone_n = 0
    sentiment_sum = 0.0
    pos_n = neu_n = neg_n = 0
    cat_counts = Counter()   # grateful / polite / accusatory / profane / contentious
    done = 0

    for it in ordered:
        nid = it.get("nid")
        if not nid:
            continue
        created = as_int(it.get("created"))
        cyear = (dt.datetime.fromtimestamp(created, tz=timezone.utc).year
                 if created else None)
        a_author = author_id(it)
        try:
            data = client.get(f"{API}/comment.json", {"node": nid})
            comments = data.get("list") or []
        except Exception:
            comments = []

        # Build an ordered event list.
        events = []
        for c in comments:
            ct = as_int(c.get("created"))
            if ct is None:
                continue
            aid = comment_author_id(c)
            nm = c.get("name")
            if aid is not None and nm and nm != "System Message":
                commenter_names.setdefault(aid, nm)
            events.append({
                "t": ct, "author": aid, "name": nm,
                "status": comment_status(c), "body": comment_body_text(c)})
        events.sort(key=lambda e: e["t"])

        # Per-issue patch / MR detection.
        has_patch = has_mr = False
        files = it.get("field_issue_files")
        if isinstance(files, list):
            for f in files:
                name = (f.get("file", {}) or {}).get("filename", "") \
                    if isinstance(f, dict) else ""
                if PATCH_RE.search(str(name)):
                    has_patch = True

        first_other = None
        first_maint = None
        heat = 0
        prev = None  # (t, status)
        saw_status = False
        was_closed = False
        reopened = False
        for e in events:
            if e["author"] is not None:
                commenter_c[e["author"]] += 1
            # first response (anyone but the author)
            if (first_other is None and e["author"] is not None
                    and e["author"] != a_author):
                first_other = e["t"]
            # first maintainer reply
            if (first_maint is None and maintainers
                    and e["author"] in maintainers and e["author"] != a_author):
                first_maint = e["t"]
            # civility + tone (human comments only; requires --tone)
            if e["body"]:
                bodies_seen += 1
                comments_scanned += 1
                s = contention_score(e["body"])
                heat += s
                if s >= 2:
                    heated_comments += 1
                if tone_scorer and (e["name"] or "") not in BOT_NAMES:
                    tone_n += 1
                    comp = tone_scorer.compound(e["body"])
                    sentiment_sum += comp
                    if comp >= 0.05:
                        pos_n += 1
                    elif comp <= -0.05:
                        neg_n += 1
                    else:
                        neu_n += 1
                    for cat, hit in categorize_tone(e["body"]).items():
                        if hit:
                            cat_counts[cat] += 1
            # MR detection from body
            if e["body"] and MR_RE.search(e["body"]):
                has_mr = True
            if e["body"] and PATCH_RE.search(e["body"]):
                has_patch = True
            # status timeline
            if e["status"] is not None:
                saw_status = True
                status_snapshots_seen += 1
                if prev is not None and prev[0] is not None:
                    dwell[prev[1]].append((e["t"] - prev[0]) / DAY)
                # reopen
                if was_closed and e["status"] not in CLOSED_STATUSES:
                    reopened = True
                if e["status"] in CLOSED_STATUSES:
                    was_closed = True
                prev = (e["t"], e["status"])

        # review-state responsiveness: for each event that *set* NR/RTBC,
        # time to the next event (any author).
        for i, e in enumerate(events):
            if e["status"] in (8, 14) and i + 1 < len(events):
                review_response.append((events[i+1]["t"] - e["t"]) / DAY)

        if created and first_other and first_other >= created:
            first_response.append((first_other - created) / DAY)
        if created and first_maint and first_maint >= created:
            maint_first_reply.append((first_maint - created) / DAY)
        if saw_status:
            reopen_observable += 1
            if reopened:
                reopen_issues += 1
            # dwell for the final status until now if still open
            if prev and prev[1] not in CLOSED_STATUSES:
                dwell[prev[1]].append((NOW - prev[0]) / DAY)
        if cyear:
            total_year[cyear] += 1
            if has_patch:
                patch_year[cyear] += 1
            if has_mr:
                mr_year[cyear] += 1
        if heat:
            issue_heat.append((heat, it.get("title", ""), issue_url(it)))
        done += 1
        if verbose and done % 10 == 0:
            print(f"  …comments for {done}/{len(ordered)} issues",
                  file=sys.stderr)

    # Clean commenter counter (drop the None key / zero artifacts).
    commenter_c = Counter({k: v for k, v in commenter_c.items()
                           if k is not None and v > 0})

    out = {"commenter_counts": commenter_c, "sampled": len(ordered),
           "commenter_names": commenter_names,
           "history_available": status_snapshots_seen > 0}
    if first_response:
        out["first_response_median"] = statistics.median(first_response)
        out["first_response_mean"] = statistics.mean(first_response)
        out["first_response_hist"] = response_histogram(first_response)
    out["commenter_gini"] = gini(commenter_c.values())
    out["commenter_top3"] = top_share(commenter_c, 3)
    out["commenter_unique"] = len([v for v in commenter_c.values() if v])
    out["reopen_observable"] = reopen_observable
    out["reopen_issues"] = reopen_issues

    # Time-in-status (median dwell per status, sorted by frequency).
    out["dwell"] = {STATUS.get(k, f"#{k}"): statistics.median(v)
                    for k, v in dwell.items() if v}
    out["dwell_n"] = {STATUS.get(k, f"#{k}"): len(v) for k, v in dwell.items()}

    # Review-state responsiveness.
    if review_response:
        out["review_response_median"] = statistics.median(review_response)
        out["review_response_n"] = len(review_response)
        out["review_response_hist"] = response_histogram(review_response)
    if maint_first_reply:
        out["maint_first_reply_median"] = statistics.median(maint_first_reply)
        out["maint_first_reply_n"] = len(maint_first_reply)

    # Patch vs MR mix over time.
    out["patch_year"] = dict(sorted(patch_year.items()))
    out["mr_year"] = dict(sorted(mr_year.items()))
    out["total_year_deep"] = dict(sorted(total_year.items()))

    # Civility heuristic.
    out["comments_scanned"] = comments_scanned
    out["heated_comments"] = heated_comments
    out["bodies_seen"] = bodies_seen
    out["status_snapshots_seen"] = status_snapshots_seen

    # Module tone profile (aggregate, multi-dimensional).
    if tone_n:
        mean_comp = sentiment_sum / tone_n
        grat = cat_counts["grateful"] / tone_n
        polite = cat_counts["polite"] / tone_n
        friction = (cat_counts["contentious"] + cat_counts["accusatory"]
                    + cat_counts["profane"]) / tone_n
        friction = min(1.0, friction)
        sentiment01 = (mean_comp + 1) / 2
        kindness = min(1.0, grat * 2)
        index = 100 * (0.55 * sentiment01 + 0.20 * kindness
                       + 0.25 * (1 - friction))
        index = max(0.0, min(100.0, index))
        out["tone"] = {
            "engine": tone_scorer.engine, "n": tone_n,
            "mean_compound": mean_comp,
            "pos_rate": pos_n / tone_n, "neu_rate": neu_n / tone_n,
            "neg_rate": neg_n / tone_n,
            "gratitude_rate": grat, "politeness_rate": polite,
            "contention_rate": cat_counts["contentious"] / tone_n,
            "accusation_rate": cat_counts["accusatory"] / tone_n,
            "profanity_rate": cat_counts["profane"] / tone_n,
            "friction_rate": friction,
            "civility_index": index, "band": civility_band(index),
        }
    issue_heat.sort(reverse=True)
    out["top_heated_issues"] = issue_heat[:8]
    return out



def response_histogram(days_list):
    buckets = Counter()
    order = ["≤1 day", "1–7 days", "1–4 weeks", "1–6 months", ">6 months"]
    for d in days_list:
        if d <= 1:
            buckets["≤1 day"] += 1
        elif d <= 7:
            buckets["1–7 days"] += 1
        elif d <= 30:
            buckets["1–4 weeks"] += 1
        elif d <= 182:
            buckets["1–6 months"] += 1
        else:
            buckets[">6 months"] += 1
    return [(k, buckets.get(k, 0)) for k in order]


# ---------------------------------------------------------------------------
# Inline SVG charts (self-contained, print-friendly — no JS, no CDN).
# ---------------------------------------------------------------------------
PALETTE = ["#2b6cb0", "#3182ce", "#4299e1", "#63b3ed", "#90cdf4", "#bee3f8"]
INK = "#1a202c"
MUTED = "#718096"
GRID = "#e2e8f0"


def hbar(pairs, width=520, bar_h=26, gap=10, color="#3182ce", fmt=None):
    """Horizontal bar chart from list of (label, value)."""
    pairs = [p for p in pairs if p[1]]
    if not pairs:
        return "<p class='muted'>No data.</p>"
    fmt = fmt or (lambda v: f"{v:,}")
    maxv = max(v for _, v in pairs)
    label_w = 190
    chart_w = width - label_w - 70
    h = len(pairs) * (bar_h + gap) + gap
    rows = []
    for i, (label, v) in enumerate(pairs):
        y = gap + i * (bar_h + gap)
        bw = max(2, (v / maxv) * chart_w) if maxv else 2
        rows.append(
            f'<text x="{label_w-8}" y="{y+bar_h*0.68}" text-anchor="end" '
            f'class="lbl">{escape(str(label))}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bw:.1f}" height="{bar_h}" '
            f'rx="3" fill="{color}"/>'
            f'<text x="{label_w+bw+6:.1f}" y="{y+bar_h*0.68}" '
            f'class="val">{escape(fmt(v))}</text>')
    return (f'<svg viewBox="0 0 {width} {h}" class="chart" '
            f'role="img">{"".join(rows)}</svg>')


def area_trend(created_by_year, closed_by_year, width=620, height=240):
    years = sorted(set(created_by_year) | set(closed_by_year))
    if not years:
        return "<p class='muted'>No data.</p>"
    years = list(range(min(years), max(years) + 1))
    cre = [created_by_year.get(y, 0) for y in years]
    clo = [closed_by_year.get(y, 0) for y in years]
    maxv = max(cre + clo + [1])
    pad_l, pad_b, pad_t, pad_r = 44, 30, 26, 14
    cw = width - pad_l - pad_r
    ch = height - pad_b - pad_t
    n = len(years)
    step = cw / max(1, n - 1) if n > 1 else cw

    def pts(vals):
        if n == 1:
            x = pad_l + cw / 2
            y = pad_t + ch - (vals[0] / maxv) * ch
            return f"{x:.1f},{y:.1f}"
        return " ".join(
            f"{pad_l + i*step:.1f},{pad_t + ch - (v/maxv)*ch:.1f}"
            for i, v in enumerate(vals))

    grid = []
    for g in range(5):
        gy = pad_t + ch - (g / 4) * ch
        gv = maxv * g / 4
        grid.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width-pad_r}" '
                    f'y2="{gy:.1f}" stroke="{GRID}"/>'
                    f'<text x="{pad_l-6}" y="{gy+3:.1f}" text-anchor="end" '
                    f'class="axis">{gv:.0f}</text>')
    xlabels = []
    show_every = max(1, n // 10)
    for i, y in enumerate(years):
        if i % show_every == 0 or i == n - 1:
            x = pad_l + (cw / 2 if n == 1 else i * step)
            xlabels.append(f'<text x="{x:.1f}" y="{height-8}" '
                           f'text-anchor="middle" class="axis">{y}</text>')
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">'
        f'{"".join(grid)}'
        f'<polyline fill="none" stroke="#2b6cb0" stroke-width="2.5" '
        f'points="{pts(cre)}"/>'
        f'<polyline fill="none" stroke="#dd6b20" stroke-width="2.5" '
        f'stroke-dasharray="5 4" points="{pts(clo)}"/>'
        f'{"".join(xlabels)}'
        f'<g class="legend">'
        f'<rect x="{pad_l}" y="2" width="11" height="11" fill="#2b6cb0"/>'
        f'<text x="{pad_l+16}" y="11" class="axis">Opened</text>'
        f'<rect x="{pad_l+90}" y="2" width="11" height="11" fill="#dd6b20"/>'
        f'<text x="{pad_l+106}" y="11" class="axis">Resolved</text>'
        f'</g></svg>')


def line_series(pairs, width=620, height=210, unit="days", color="#805ad5"):
    """Single-series line chart from list of (label, value)."""
    pairs = [(str(k), v) for k, v in pairs if v is not None]
    if not pairs:
        return "<p class='muted'>Not enough data.</p>"
    vals = [v for _, v in pairs]
    maxv = max(vals + [1])
    pad_l = 52 if unit == "days" else 44
    pad_b, pad_t, pad_r = 28, 14, 14
    cw, ch = width - pad_l - pad_r, height - pad_b - pad_t
    n = len(pairs)
    step = cw / max(1, n - 1) if n > 1 else cw
    coords = [(pad_l + (cw / 2 if n == 1 else i * step),
               pad_t + ch - (v / maxv) * ch) for i, (_, v) in enumerate(pairs)]
    grid = []
    for g in range(5):
        gy = pad_t + ch - (g / 4) * ch
        gv = maxv * g / 4
        lbl = human_days_short(gv) if unit == "days" else f"{gv:.0f}"
        grid.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width-pad_r}" '
                    f'y2="{gy:.1f}" stroke="{GRID}"/>'
                    f'<text x="{pad_l-6}" y="{gy+3:.1f}" text-anchor="end" '
                    f'class="axis">{escape(lbl)}</text>')
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
                   for x, y in coords)
    show_every = max(1, n // 10)
    xlabels = "".join(
        f'<text x="{coords[i][0]:.1f}" y="{height-8}" text-anchor="middle" '
        f'class="axis">{escape(pairs[i][0])}</text>'
        for i in range(n) if i % show_every == 0 or i == n - 1)
    return (f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">'
            f'{"".join(grid)}'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
            f'points="{poly}"/>{dots}{xlabels}</svg>')


def donut(open_n, closed_n, size=150):
    total = open_n + closed_n
    if total == 0:
        return ""
    r, cx, cy, sw = 56, size / 2, size / 2, 22
    circ = 2 * math.pi * r
    open_frac = open_n / total
    open_len = circ * open_frac
    return (
        f'<svg viewBox="0 0 {size} {size}" class="donut" role="img">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#dd6b20" '
        f'stroke-width="{sw}"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#2b6cb0" '
        f'stroke-width="{sw}" stroke-dasharray="{open_len:.1f} {circ:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>'
        f'<text x="{cx}" y="{cy-2}" text-anchor="middle" class="donut-big">'
        f'{open_frac*100:.0f}%</text>'
        f'<text x="{cx}" y="{cy+16}" text-anchor="middle" class="donut-sub">'
        f'open</text></svg>')


# Per-project colors used across the comparison report.
PROJECT_COLORS = ["#2b6cb0", "#dd6b20", "#38a169", "#805ad5"]


def grouped_hbar(rows, labels, colors, width=620, bar_h=13, group_gap=16,
                 fmt=None):
    """Grouped horizontal bars. rows = [(category, [v_proj1, v_proj2, ...])]."""
    fmt = fmt or (lambda v: f"{v:.0f}%")
    rows = [r for r in rows if any(v for v in r[1])]
    if not rows:
        return "<p class='muted'>No data.</p>"
    maxv = max(max(vs) for _, vs in rows) or 1
    n = len(labels)
    label_w, val_w = 168, 56
    chart_w = width - label_w - val_w
    group_h = n * bar_h + (n - 1) * 2
    h = len(rows) * (group_h + group_gap) + group_gap
    out = []
    y = group_gap
    for cat, vs in rows:
        out.append(f'<text x="{label_w-8}" y="{y+group_h/2+4:.1f}" '
                   f'text-anchor="end" class="lbl">{escape(str(cat))}</text>')
        for j, v in enumerate(vs):
            by = y + j * (bar_h + 2)
            bw = max(1.5, (v / maxv) * chart_w)
            out.append(
                f'<rect x="{label_w}" y="{by:.1f}" width="{bw:.1f}" '
                f'height="{bar_h}" rx="2" fill="{colors[j]}"/>'
                f'<text x="{label_w+bw+5:.1f}" y="{by+bar_h*0.82:.1f}" '
                f'class="val">{escape(fmt(v))}</text>')
        y += group_h + group_gap
    return (f'<svg viewBox="0 0 {width} {h}" class="chart" role="img">'
            f'{"".join(out)}</svg>')


def multi_line(series, width=640, height=250, unit="count"):
    """Overlaid line chart. series = [(label, color, [(x,y),...]), ...]."""
    series = [(lbl, col, [(x, y) for x, y in pts if y is not None])
              for lbl, col, pts in series]
    series = [s for s in series if s[2]]
    if not series:
        return "<p class='muted'>Not enough data.</p>"
    xs = sorted({x for _, _, pts in series for x, _ in pts})
    ys = [y for _, _, pts in series for _, y in pts]
    xmin, xmax = min(xs), max(xs)
    maxv = max(ys + [1])
    pad_l = 52 if unit == "days" else 44
    pad_b, pad_t, pad_r = 28, 30, 14
    cw, ch = width - pad_l - pad_r, height - pad_b - pad_t
    span = max(1, xmax - xmin)

    def px(x):
        return pad_l + (x - xmin) / span * cw

    def py(y):
        return pad_t + ch - (y / maxv) * ch

    grid = []
    for g in range(5):
        gy = pad_t + ch - (g / 4) * ch
        gv = maxv * g / 4
        lbl = human_days_short(gv) if unit == "days" else f"{gv:.0f}"
        grid.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width-pad_r}" '
                    f'y2="{gy:.1f}" stroke="{GRID}"/>'
                    f'<text x="{pad_l-6}" y="{gy+3:.1f}" text-anchor="end" '
                    f'class="axis">{escape(lbl)}</text>')
    show_every = max(1, len(xs) // 10)
    xlabels = "".join(
        f'<text x="{px(x):.1f}" y="{height-8}" text-anchor="middle" '
        f'class="axis">{x}</text>'
        for i, x in enumerate(xs) if i % show_every == 0 or i == len(xs) - 1)
    lines, legend = [], []
    for k, (lbl, col, pts) in enumerate(series):
        pts = sorted(pts)
        poly = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
        dots = "".join(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.5" '
                       f'fill="{col}"/>' for x, y in pts)
        lines.append(f'<polyline fill="none" stroke="{col}" stroke-width="2.5" '
                     f'points="{poly}"/>{dots}')
        lx = pad_l + k * 150
        legend.append(f'<rect x="{lx}" y="6" width="11" height="11" '
                      f'fill="{col}"/><text x="{lx+16}" y="15" '
                      f'class="axis">{escape(lbl)}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">'
            f'{"".join(grid)}{"".join(lines)}{"".join(xlabels)}'
            f'<g class="legend">{"".join(legend)}</g></svg>')


# ---------------------------------------------------------------------------
# HTML report.
# ---------------------------------------------------------------------------
CSS = """
:root{--ink:#1a202c;--muted:#718096;--line:#e2e8f0;--accent:#2b6cb0;
--accent2:#dd6b20;--bg:#fff;--soft:#f7fafc;}
*{box-sizing:border-box;}
body{margin:0;color:var(--ink);background:#eef1f5;
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,
Arial,sans-serif;}
.page{max-width:880px;margin:24px auto;background:var(--bg);
padding:48px 56px;box-shadow:0 1px 4px rgba(0,0,0,.08);}
h1{font-size:30px;margin:0 0 2px;letter-spacing:-.01em;}
h2{font-size:19px;margin:38px 0 14px;padding-bottom:7px;
border-bottom:2px solid var(--ink);letter-spacing:-.01em;}
h3{font-size:14px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);margin:22px 0 8px;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
.sub{color:var(--muted);margin:0 0 4px;font-size:14px;}
.muted{color:var(--muted);}
.eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:11px;
color:var(--accent);font-weight:600;margin:0 0 6px;}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0;}
.card{background:var(--soft);border:1px solid var(--line);border-radius:8px;
padding:14px 16px;}
.card .n{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1.1;}
.card .k{font-size:12px;color:var(--muted);text-transform:uppercase;
letter-spacing:.05em;margin-top:3px;}
.split{display:grid;grid-template-columns:150px 1fr;gap:24px;align-items:center;}
table{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:14px;}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
.chart{width:100%;height:auto;}
.chart .lbl{font-size:12px;fill:var(--ink);}
.chart .val{font-size:11px;fill:var(--muted);font-variant-numeric:tabular-nums;}
.chart .axis{font-size:10px;fill:var(--muted);}
.chart .legend text{font-size:10px;fill:var(--muted);}
.donut .donut-big{font-size:24px;font-weight:700;fill:var(--ink);}
.donut .donut-sub{font-size:11px;fill:var(--muted);}
.note{background:#fffaf0;border:1px solid #fbd38d;border-radius:8px;
padding:12px 16px;font-size:13px;color:#744210;margin:14px 0;}
.reco{background:var(--soft);border:1px solid var(--line);border-radius:8px;
padding:16px 20px;margin:10px 0;}
.reco h4{margin:0 0 4px;font-size:14px;}
.reco p{margin:0;font-size:13px;color:var(--muted);}
.foot{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
font-size:12px;color:var(--muted);}
.tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;
background:#ebf8ff;color:#2c5282;}
.score td,.score th{text-align:right;}
.score td:first-child,.score th:first-child{text-align:left;}
.score td.best{font-weight:700;color:#276749;background:#f0fff4;}
.score .dir{font-size:10px;color:var(--muted);font-weight:400;}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:6px;vertical-align:baseline;}
.legend-row{font-size:13px;margin:6px 0 0;}
.refs{margin-top:18px;padding-top:12px;border-top:1px solid var(--line);
font-size:11px;color:var(--muted);line-height:1.5;}
.refs h3{margin:0 0 6px;color:var(--muted);}
.refs ol{margin:0;padding-left:18px;}
.refs li{margin:2px 0;}
.refs a{color:var(--muted);text-decoration:underline;}
@media print{
 body{background:#fff;}
 .page{box-shadow:none;margin:0;max-width:100%;padding:0 8mm;}
 h2{page-break-after:avoid;}
 table,.cards,.split,.chart,.reco{page-break-inside:avoid;}
 a{color:var(--ink);}
}
@media(max-width:640px){.cards{grid-template-columns:repeat(2,1fr);}
.split{grid-template-columns:1fr;}}
"""


def card(n, k):
    return f'<div class="card"><div class="n">{n}</div><div class="k">{escape(k)}</div></div>'


def counter_table(counter, head, total=None, top=None):
    items = counter.most_common(top) if top else sorted(
        counter.items(), key=lambda x: x[1], reverse=True)
    total = total or sum(counter.values())
    rows = []
    for k, v in items:
        pct = f"{v/total*100:.1f}%" if total else "—"
        rows.append(f"<tr><td>{escape(str(k))}</td>"
                    f"<td class='num'>{v:,}</td>"
                    f"<td class='num muted'>{pct}</td></tr>")
    return (f"<table><thead><tr><th>{escape(head)}</th>"
            f"<th class='num'>Issues</th><th class='num'>Share</th></tr>"
            f"</thead><tbody>{''.join(rows)}</tbody></table>")


def references_html(deeps):
    """Build a references block. `deeps` is a list of deep-result dicts (may
    contain None). VADER is cited only when it was the engine actually used."""
    deeps = [d for d in deeps if d]
    refs = []
    used_vader = any((d.get("tone") or {}).get("engine", "").startswith("VADER")
                     for d in deeps)
    used_fallback = any(
        (d.get("tone") or {}).get("engine") == "built-in lexicon"
        for d in deeps)
    tone_present = any(d.get("tone") for d in deeps)

    if used_vader:
        refs.append(
            "Hutto, C.J. &amp; Gilbert, E.E. (2014). "
            "<em>VADER: A Parsimonious Rule-based Model for Sentiment Analysis "
            "of Social Media Text.</em> Proceedings of the Eighth "
            "International AAAI Conference on Weblogs and Social Media "
            "(ICWSM-14), Ann Arbor, MI. Implementation: "
            "<a href='https://github.com/cjhutto/vaderSentiment'>"
            "cjhutto/vaderSentiment</a>. Used here for comment sentiment.")
    elif used_fallback and tone_present:
        refs.append(
            "Sentiment was computed with this tool's <em>built-in lexicon "
            "fallback</em> (the VADER package was not installed), which "
            "approximates but is not VADER. Install <code>vaderSentiment</code> "
            "for the published method: "
            "<a href='https://github.com/cjhutto/vaderSentiment'>"
            "cjhutto/vaderSentiment</a> (Hutto &amp; Gilbert, 2014).")
    if tone_present:
        refs.append(
            "Gratitude, politeness and friction rates are lexicon heuristics "
            "defined in this tool, not a published instrument.")
        refs.append(
            "Framing of discussion tone as a community-health signal follows "
            "<em>CHAOSS — Community Health Analytics in Open Source "
            "Software</em> (Linux Foundation), "
            "<a href='https://chaoss.community/'>chaoss.community</a>.")
    refs.append(
        "Issue and comment data: Drupal.org REST API (api-d7), "
        "<a href='https://www.drupal.org/drupalorg/docs/apis/rest-and-other-apis'>"
        "drupal.org/drupalorg/docs/apis</a>.")
    items = "".join(f"<li>{r}</li>" for r in refs)
    return (f"<div class='refs'><h3>References &amp; data sources</h3>"
            f"<ol>{items}</ol></div>")


def render(project, machine, stats, deep, opts):
    created_ts = as_int(project.get("created"))
    age_days = (NOW - created_ts) / DAY if created_ts else None
    created_str = (dt.datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%B %Y")
                   if created_ts else "unknown")
    title = escape(project.get("title", machine))
    gen = dt.datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    proj_url = f"https://www.drupal.org/project/{machine}"

    h = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>{title} — issue queue report</title>",
         f"<style>{CSS}</style></head><body><div class='page'>"]

    h.append(f"<p class='eyebrow'>Drupal.org issue-queue report</p>")
    h.append(f"<h1>{title}</h1>")
    h.append(f"<p class='sub'><a href='{proj_url}'>{escape(machine)}</a> · "
             f"project created {created_str} · "
             f"{human_days(age_days)} old</p>")

    if stats["total"] == 0:
        h.append("<p>No issues found in this queue.</p></div></body></html>")
        return "".join(h)

    # ---- Summary cards
    h.append("<div class='cards'>")
    h.append(card(f"{stats['total']:,}", "Total issues"))
    h.append(card(f"{stats['open_count']:,}", "Open / unresolved"))
    h.append(card(f"{stats['closed_count']:,}", "Resolved / closed"))
    h.append(card(f"{stats['comment_total']:,}", "Total comments"))
    h.append("</div>")
    h.append("<div class='cards'>")
    h.append(card(human_days(stats.get("res_median")), "Median time to close"))
    h.append(card(human_days(stats.get("open_median_age")),
                  "Median age, open issues"))
    h.append(card(f"{stats['comment_avg']:.1f}", "Avg comments / issue"))
    h.append(card(f"{stats['recent90']:,}", "Touched in last 90 days"))
    h.append("</div>")

    # ---- Open vs closed
    h.append("<h2>Open vs. resolved</h2>")
    h.append("<div class='split'>")
    h.append(donut(stats["open_count"], stats["closed_count"]))
    h.append("<div>")
    h.append(counter_table(stats["status"], "Status", stats["total"]))
    h.append("</div></div>")

    # ---- Activity over time
    h.append("<h2>Activity over time</h2>")
    h.append("<p class='sub'>Issues opened vs. resolved per year. "
             "Resolution year uses each issue's last-changed date as a "
             "proxy for when it closed.</p>")
    h.append(area_trend(stats["created_year"], stats["closed_year"]))

    # ---- Resolution-time trend (by opened-year cohort)
    if stats.get("cohort_res"):
        h.append("<h2>Resolution-time trend</h2>")
        h.append("<p class='sub'>Median days to close, grouped by the year an "
                 "issue was opened (cohorts of 4+ resolved issues). Shows "
                 "whether the project is getting faster or slower over "
                 "time.</p>")
        h.append(line_series(
            [(yr, med) for yr, (med, _) in
             sorted(stats["cohort_res"].items())], unit="days",
            color="#dd6b20"))

    # ---- Priority / category
    h.append("<h2>Priority &amp; category</h2>")
    h.append("<h3>By priority</h3>")
    h.append(hbar([(k, stats["priority"][k]) for k in
                   ["Critical", "Major", "Normal", "Minor", "Unspecified"]
                   if stats["priority"].get(k)], color="#c53030"))
    h.append("<h3>By category</h3>")
    h.append(hbar(stats["category"].most_common(), color="#2b6cb0"))

    # ---- Versions
    h.append("<h2>Drupal versions</h2>")
    h.append("<h3>By core branch</h3>")
    h.append(hbar(stats["branch"].most_common(10), color="#319795"))
    if len(stats["version"]) > 1:
        h.append("<h3>Top exact version targets</h3>")
        h.append(counter_table(stats["version"], "Version", stats["total"],
                               top=12))
    if stats.get("open_branch_posture"):
        h.append("<h3>Open issues by support posture</h3>")
        h.append("<p class='sub'>Open issues filed against current vs. "
                 "end-of-life Drupal branches (heuristic — see notes).</p>")
        order = ["current", "current (semver)", "legacy / EOL", "unspecified"]
        colors = {"current": "#38a169", "current (semver)": "#38a169",
                  "legacy / EOL": "#c53030", "unspecified": "#a0aec0"}
        h.append(hbar([(k, stats["open_branch_posture"].get(k, 0))
                       for k in order if stats["open_branch_posture"].get(k)],
                      color="#38a169"))
        legacy = (stats["open_branch_posture"].get("legacy / EOL", 0))
        if stats["open_count"]:
            h.append(f"<p class='sub'>{legacy:,} of {stats['open_count']:,} "
                     f"open issues "
                     f"({legacy/stats['open_count']*100:.1f}%) target a "
                     f"legacy/EOL branch.</p>")

    # ---- Components
    if len([k for k in stats["component"] if k != "unspecified"]) > 0:
        h.append("<h2>Components</h2>")
        h.append(hbar(stats["component"].most_common(12), color="#805ad5"))

    # ---- Contributors
    h.append("<h2>Most active issue authors</h2>")
    h.append("<p class='sub'>By number of issues opened. Usernames resolved "
             "for the top contributors.</p>")
    top_authors = stats["author_counts"].most_common(15)
    if opts["resolve"] and top_authors:
        for uid, _ in top_authors:
            if uid not in stats["author_names"]:
                stats["author_names"][uid] = resolve_username(
                    opts["client"], uid, opts["uname_cache"])
    rows = []
    for uid, c in top_authors:
        name = stats["author_names"].get(uid, f"uid:{uid}")
        link = f"https://www.drupal.org/user/{uid}"
        rows.append(f"<tr><td><a href='{link}'>{escape(name)}</a></td>"
                    f"<td class='num'>{c:,}</td></tr>")
    h.append(f"<table><thead><tr><th>Contributor</th>"
             f"<th class='num'>Issues opened</th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table>")

    # ---- Contribution concentration (bus factor)
    if stats.get("author_gini") is not None:
        h.append("<h2>Contribution concentration</h2>")
        h.append("<p class='sub'>How evenly issue-reporting is spread across "
                 "contributors. A high Gini or top-3 share means the project "
                 "leans heavily on a few people (low bus factor).</p>")
        h.append("<div class='cards'>")
        h.append(card(f"{stats['author_unique']:,}", "Unique authors"))
        h.append(card(f"{stats['author_gini']:.2f}", "Gini (0=even, 1=concentrated)"))
        h.append(card(f"{stats['author_top3']*100:.0f}%", "Share from top 3"))
        h.append(card(f"{stats['author_top10']*100:.0f}%", "Share from top 10"))
        h.append("</div>")
    if deep:
        h.append("<h2>Discussion &amp; engagement (deep mode)</h2>")
        h.append(f"<p class='sub'>Comment-level analysis of the "
                 f"{deep['sampled']} most-discussed issues.</p>")
        if "first_response_median" in deep:
            h.append("<div class='cards'>")
            h.append(card(human_days(deep["first_response_median"]),
                          "Median time to first response"))
            h.append(card(human_days(deep["first_response_mean"]),
                          "Mean time to first response"))
            h.append("</div>")
            if deep.get("first_response_hist"):
                h.append("<h3>Time to first response — distribution</h3>")
                h.append(hbar(deep["first_response_hist"], color="#319795"))
        if deep.get("commenter_gini") is not None:
            h.append("<h3>Commenter concentration</h3>")
            h.append("<div class='cards'>")
            h.append(card(f"{deep['commenter_unique']:,}",
                          "Unique commenters (sampled)"))
            h.append(card(f"{deep['commenter_gini']:.2f}",
                          "Gini (0=even, 1=concentrated)"))
            h.append(card(f"{(deep['commenter_top3'] or 0)*100:.0f}%",
                          "Share from top 3"))
            h.append("</div>")
        # Reopen rate — only when status history could be reconstructed.
        if deep.get("history_available") and deep.get("reopen_observable"):
            h.append("<h3>Reopen rate</h3>")
            obs = deep["reopen_observable"]
            ro = deep["reopen_issues"]
            h.append(f"<p class='sub'>{ro:,} of {obs:,} sampled issues whose "
                     f"status history could be reconstructed were reopened "
                     f"after being closed ({ro/obs*100:.1f}%).</p>")
        top_c = deep["commenter_counts"].most_common(15)
        for uid, nm in deep.get("commenter_names", {}).items():
            stats["author_names"].setdefault(uid, nm)
        if opts["resolve"]:
            for uid, _ in top_c:
                if uid not in stats["author_names"]:
                    stats["author_names"][uid] = resolve_username(
                        opts["client"], uid, opts["uname_cache"])
        rows = []
        for uid, c in top_c:
            name = stats["author_names"].get(uid, f"uid:{uid}")
            link = f"https://www.drupal.org/user/{uid}"
            rows.append(f"<tr><td><a href='{link}'>{escape(name)}</a></td>"
                        f"<td class='num'>{c:,}</td></tr>")
        h.append("<h3>Most engaged commenters (sampled)</h3>")
        h.append(f"<table><thead><tr><th>Contributor</th>"
                 f"<th class='num'>Comments</th></tr></thead>"
                 f"<tbody>{''.join(rows)}</tbody></table>")

        # ---- Time-in-status (needs per-comment status history)
        if deep.get("dwell"):
            h.append("<h3>Time spent in each status</h3>")
            h.append("<p class='sub'>Median time issues dwell in a status "
                     "before moving on, reconstructed from status snapshots "
                     "on comments. Reveals where work stalls.</p>")
            order = ["Active", "Needs work", "Needs review",
                     "Reviewed & tested by the community", "Patch (to be ported)",
                     "Postponed", "Postponed (maintainer needs more info)",
                     "Fixed"]
            pairs = [(k, deep["dwell"][k]) for k in order if k in deep["dwell"]]
            pairs += [(k, v) for k, v in deep["dwell"].items()
                      if k not in dict(pairs)]
            h.append(hbar([(k, round(v, 1)) for k, v in pairs],
                          color="#dd6b20",
                          fmt=lambda v: human_days_short(v)))
        elif not deep.get("history_available"):
            h.append("<h3>Time-in-status &amp; reopen history</h3>")
            h.append("<p class='sub muted'>Unavailable: the api-d7 comment "
                     "feed doesn't include the per-comment status snapshots "
                     "needed to reconstruct status transitions, so "
                     "time-in-status and reopen rate can't be computed from "
                     "this source. They'd require scraping each issue's "
                     "revision log.</p>")

        # ---- Responsiveness
        if deep.get("review_response_median") is not None:
            h.append("<h3>Review-state responsiveness</h3>")
            h.append(f"<p class='sub'>After an issue is set to "
                     f"<em>Needs review</em> or <em>RTBC</em>, median time to "
                     f"the next reply: <strong>"
                     f"{human_days(deep['review_response_median'])}</strong> "
                     f"(over {deep['review_response_n']:,} transitions). The "
                     f"queue's review throughput.</p>")
            if deep.get("review_response_hist"):
                h.append(hbar(deep["review_response_hist"], color="#319795"))
        if deep.get("maint_first_reply_median") is not None:
            h.append("<h3>Maintainer responsiveness</h3>")
            h.append(f"<p class='sub'>Median time from issue creation to the "
                     f"first reply by a known maintainer: <strong>"
                     f"{human_days(deep['maint_first_reply_median'])}</strong> "
                     f"(over {deep['maint_first_reply_n']:,} issues with a "
                     f"maintainer reply).</p>")
        elif deep.get("review_response_median") is not None:
            h.append("<p class='sub muted'>Maintainer-specific responsiveness "
                     "needs the maintainer list — pass <code>--maintainers "
                     "uid1,uid2</code> to enable it.</p>")

        # ---- Patch vs merge-request mix
        if deep.get("patch_year") or deep.get("mr_year"):
            h.append("<h3>Patch vs. merge-request adoption</h3>")
            h.append("<p class='sub'>Issues using an uploaded patch vs. a "
                     "GitLab merge request, by the year the issue was opened "
                     "(sampled). Shows the shift to the modern workflow. "
                     "MR detection is heuristic (scans comments for fork/MR "
                     "markers).</p>")
            years = sorted(set(deep.get("patch_year", {}))
                           | set(deep.get("mr_year", {})))
            if years:
                h.append(multi_line([
                    ("Patch", "#dd6b20",
                     [(y, deep.get("patch_year", {}).get(y, 0)) for y in years]),
                    ("Merge request", "#2b6cb0",
                     [(y, deep.get("mr_year", {}).get(y, 0)) for y in years]),
                ], unit="count"))

        # ---- Civility heuristic
        # ---- Module tone & civility profile
        t = deep.get("tone")
        if t:
            h.append("<h3>Module tone &amp; civility profile</h3>")
            h.append(f"<p class='sub'>An aggregate read on how discussion in "
                     f"this queue tends to feel, over {t['n']:,} human "
                     f"comments. Sentiment engine: <strong>{escape(t['engine'])}"
                     f"</strong>.</p>")
            h.append("<div class='split'>")
            band = t["band"]
            band_color = ("#38a169" if t["civility_index"] >= 62
                          else "#dd6b20" if t["civility_index"] >= 48
                          else "#c53030")
            h.append(
                f"<div style='text-align:center'>"
                f"<div style='font-size:46px;font-weight:700;line-height:1;"
                f"color:{band_color}'>{t['civility_index']:.0f}</div>"
                f"<div class='k' style='font-size:12px;color:var(--muted);"
                f"text-transform:uppercase;letter-spacing:.05em'>Civility "
                f"index</div><div style='margin-top:4px;font-size:13px;"
                f"color:{band_color};font-weight:600'>{escape(band)}</div></div>")
            h.append("<div>")
            h.append(hbar([
                ("Positive", round(t["pos_rate"]*100, 1)),
                ("Neutral", round(t["neu_rate"]*100, 1)),
                ("Negative", round(t["neg_rate"]*100, 1)),
                ("Gratitude / kindness", round(t["gratitude_rate"]*100, 1)),
                ("Politeness markers", round(t["politeness_rate"]*100, 1)),
                ("Friction / defensive", round(t["friction_rate"]*100, 1)),
            ], color="#3182ce", fmt=lambda v: f"{v:.0f}%"))
            h.append("</div></div>")
            h.append(f"<div class='note'><strong>Read this as a rough signal, "
                     f"not a verdict.</strong> Sentiment uses "
                     f"{escape(t['engine'])}; the gratitude / politeness / "
                     f"friction rates are lexicon heuristics (share of comments "
                     f"containing each kind of marker). It is English-only and "
                     f"blind to sarcasm, quoted text, and code snippets, and it "
                     f"scores the <em>queue in aggregate — never individuals</em>. "
                     f"The index is a transparent blend: 55% mean sentiment, "
                     f"20% gratitude, 25% absence of friction (framing loosely "
                     f"follows community-health work like CHAOSS). "
                     f"Mean sentiment compound: {t['mean_compound']:+.2f} "
                     f"(−1 to +1).</div>")

        # ---- Threads to review (issue-level flags)
        if deep.get("top_heated_issues"):
            h.append("<h3>Threads flagged for manual review</h3>")
            h.append("<p class='sub'>Issues with the most friction markers — "
                     "a starting point for a human to look at, not a "
                     "conclusion.</p>")
            rows = []
            for score, ttl, url in deep["top_heated_issues"]:
                rows.append(f"<tr><td><a href='{url}'>"
                            f"{escape(ttl[:90])}</a></td>"
                            f"<td class='num'>{score}</td></tr>")
            h.append(f"<table><thead><tr><th>Issue</th>"
                     f"<th class='num'>Friction score</th></tr></thead>"
                     f"<tbody>{''.join(rows)}</tbody></table>")

    # ---- Comment distribution
    h.append("<h2>Discussion depth</h2>")
    order = ["0", "1–5", "6–10", "11–25", "26–50", "50+"]
    h.append(hbar([(k, stats["comment_buckets"].get(k, 0)) for k in order],
                  color="#3182ce"))
    h.append("<h3>Most-discussed issues</h3>")
    rows = []
    for cc, ttl, url, st in stats["top_commented"][:10]:
        rows.append(f"<tr><td><a href='{url}'>{escape(ttl[:90])}</a></td>"
                    f"<td><span class='tag'>{escape(STATUS.get(st,'?'))}"
                    f"</span></td><td class='num'>{cc:,}</td></tr>")
    h.append(f"<table><thead><tr><th>Issue</th><th>Status</th>"
             f"<th class='num'>Comments</th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table>")

    # ---- Oldest open
    h.append("<h2>Oldest open issues</h2>")
    rows = []
    for age, ttl, url, st in stats["oldest_open"][:10]:
        rows.append(f"<tr><td><a href='{url}'>{escape(ttl[:90])}</a></td>"
                    f"<td><span class='tag'>{escape(st)}</span></td>"
                    f"<td class='num'>{human_days(age)}</td></tr>")
    h.append(f"<table><thead><tr><th>Issue</th><th>Status</th>"
             f"<th class='num'>Age</th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table>")

    # ---- Maintainer bottlenecks
    if stats.get("needs_review") or stats.get("rtbc"):
        h.append("<h2>Maintainer bottlenecks</h2>")
        h.append("<p class='sub'>How long currently-open issues have been "
                 "waiting in review states (age since last change).</p>")
        h.append("<div class='cards'>")
        nr, rt = stats.get("needs_review"), stats.get("rtbc")
        if nr:
            h.append(card(f"{nr['count']:,}", "In Needs review"))
            h.append(card(human_days(nr["median"]), "Median age, Needs review"))
        if rt:
            h.append(card(f"{rt['count']:,}", "In RTBC"))
            h.append(card(human_days(rt["median"]), "Median age, RTBC"))
        h.append("</div>")
        if rt and rt["oldest"]:
            h.append(f"<p class='sub'>Oldest RTBC issue has been waiting "
                     f"{human_days(rt['oldest'])}. RTBC is the classic "
                     f"maintainer-attention bottleneck — these are reviewed "
                     f"and ready to commit.</p>")

    # ---- Seasonality
    if stats.get("by_month"):
        h.append("<h2>Seasonality</h2>")
        h.append("<h3>Issues opened by month</h3>")
        h.append(hbar([(MONTHS[m-1], stats["by_month"].get(m, 0))
                       for m in range(1, 13)], color="#2b6cb0"))
        h.append("<h3>Issues opened by weekday</h3>")
        h.append(hbar([(WEEKDAYS[w], stats["by_weekday"].get(w, 0))
                       for w in range(7)], color="#4299e1"))

    # ---- Tag themes
    if stats.get("tags"):
        top_tags = stats["tags"].most_common(15)
        if opts["resolve"]:
            for key, _ in top_tags:
                if key not in stats["tag_names"] and key[0] == "id":
                    stats["tag_names"][key] = resolve_tag_name(
                        opts["client"], key[1], opts["tag_cache"])
        labeled = [(stats["tag_names"].get(key, str(key[1] or key)), v)
                   for key, v in top_tags]
        if any(v for _, v in labeled):
            h.append("<h2>Tag themes</h2>")
            h.append("<p class='sub'>Most common issue tags — recurring "
                     "themes like accessibility, performance, or security.</p>")
            h.append(hbar(labeled, color="#805ad5"))

    # ---- Health snapshot
    h.append("<h2>Queue health snapshot</h2>")
    h.append("<div class='cards'>")
    h.append(card(f"{stats['recent30']:,}", "Touched in last 30 days"))
    h.append(card(f"{stats['stale365']:,}", "Open & stale > 1 yr"))
    if stats.get("res_count"):
        h.append(card(f"{stats['res_count']:,}", "Issues with a close time"))
    h.append(card(f"{stats['issues_with_files']:,}",
                  "Issues with attached files"))
    h.append("</div>")

    # ---- Caveats + footer
    h.append("<div class='note'><strong>Methodology notes.</strong> "
             "Data comes from the public Drupal.org api-d7 REST API. "
             "“Time to close” uses each issue’s last-modified timestamp as a "
             "proxy for its close date, so late edits to old closed issues "
             "can inflate it. “Fixed” (status 2) is counted as resolved even "
             "though it auto-closes to “Closed (fixed)” later. Author and "
             "commenter usernames are resolved only for the top contributors "
             "to keep the crawl polite. Support posture (current vs. "
             "legacy/EOL) is a heuristic on the branch number and doesn't "
             "account for a project's own supported-branch declarations. The "
             "resolution-time trend groups issues by the year they were "
             "opened, so recent cohorts contain only the faster-resolved "
             "issues and can look artificially quick.</div>")
    h.append(references_html([deep]))
    h.append(f"<div class='foot'>Generated {gen} · "
             f"{stats['total']:,} issues analyzed · "
             f"source: {proj_url}</div>")
    h.append("</div></body></html>")
    return "".join(h)


# ---------------------------------------------------------------------------
# Comparison report.
# ---------------------------------------------------------------------------
def collect_project(client, machine, args):
    """Fetch + analyze one project. Returns a bundle dict."""
    print(f"› Looking up project '{machine}'…", file=sys.stderr)
    project = get_project(client, machine)
    nid = project.get("nid")
    print(f"  found: {project.get('title')} (node {nid})", file=sys.stderr)
    print(f"› Crawling the '{machine}' issue queue…", file=sys.stderr)
    issues = list(iter_issues(client, nid, verbose=args.verbose))
    print(f"  collected {len(issues):,} issues", file=sys.stderr)
    stats = analyze(issues)
    deep = None
    if args.deep and issues:
        print(f"› Deep mode for '{machine}': comments for up to "
              f"{args.max_comment_issues} issues…", file=sys.stderr)
        maint = parse_maintainer_ids(project, args.maintainers)
        deep = deep_comments(client, issues, args.max_comment_issues,
                             verbose=args.verbose, maintainers=maint,
                             tone=args.tone)
    return {"machine": machine, "project": project, "stats": stats,
            "deep": deep, "issues_n": len(issues)}


def _pct(part, whole):
    return (part / whole * 100) if whole else None


def _age_days(project):
    c = as_int(project.get("created"))
    return (NOW - c) / DAY if c else None


def render_comparison(bundles, opts):
    cols = len(bundles)
    colors = PROJECT_COLORS[:cols]
    names = [escape(b["project"].get("title", b["machine"])) for b in bundles]
    machines = [b["machine"] for b in bundles]
    gen = dt.datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    deep_on = any(b["deep"] for b in bundles)

    h = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>{' vs '.join(names)} — comparison</title>",
         f"<style>{CSS}</style></head><body><div class='page'>"]
    h.append("<p class='eyebrow'>Drupal.org issue-queue comparison</p>")
    h.append(f"<h1>{' &nbsp;vs&nbsp; '.join(names)}</h1>")
    for i, b in enumerate(bundles):
        url = f"https://www.drupal.org/project/{b['machine']}"
        h.append(f"<p class='legend-row'><span class='swatch' "
                 f"style='background:{colors[i]}'></span>"
                 f"<a href='{url}'>{names[i]}</a> · "
                 f"{b['stats'].get('total', 0):,} issues · "
                 f"{human_days(_age_days(b['project']))} old</p>")

    # ---------- Scorecard ----------
    S = [b["stats"] for b in bundles]
    D = [b["deep"] for b in bundles]

    def row(label, direction, vals):
        """vals: list of (numeric_or_None, display_str)."""
        nums = [v[0] for v in vals]
        best_idx = set()
        present = [(i, n) for i, n in enumerate(nums) if n is not None]
        if direction and len(present) > 1:
            target = (min if direction == "lower" else max)(
                n for _, n in present)
            best_idx = {i for i, n in present if n == target}
        dirtag = (f" <span class='dir'>({'lower' if direction=='lower' else 'higher'} better)</span>"
                  if direction else "")
        cells = []
        for i, (_, disp) in enumerate(vals):
            cls = " class='best'" if i in best_idx else ""
            cells.append(f"<td{cls}>{disp}</td>")
        return f"<tr><td>{label}{dirtag}</td>{''.join(cells)}</tr>"

    h.append("<h2>Scorecard</h2>")
    h.append("<p class='sub'>Best value highlighted only where a metric has an "
             "unambiguous direction. Volume metrics are left neutral — more "
             "issues isn't inherently better or worse.</p>")
    head = "".join(f"<th>{n}</th>" for n in names)
    h.append(f"<table class='score'><thead><tr><th>Metric</th>{head}</tr>"
             f"</thead><tbody>")

    rows = []
    rows.append(row("Total issues", None,
                    [(s["total"], f"{s['total']:,}") for s in S]))
    rows.append(row("Open / unresolved", None,
                    [(s["open_count"], f"{s['open_count']:,}") for s in S]))
    rows.append(row("Resolved / closed", None,
                    [(s["closed_count"], f"{s['closed_count']:,}") for s in S]))
    rows.append(row("Median time to close", "lower",
                    [(s.get("res_median"), human_days(s.get("res_median")))
                     for s in S]))
    rows.append(row("Median age, open issues", "lower",
                    [(s.get("open_median_age"),
                      human_days(s.get("open_median_age"))) for s in S]))
    rows.append(row("Avg comments / issue", None,
                    [(s["comment_avg"], f"{s['comment_avg']:.1f}") for s in S]))
    rows.append(row("Active in last 90 days", "higher",
                    [(_pct(s["recent90"], s["total"]),
                      f"{_pct(s['recent90'], s['total']):.0f}%"
                      if s["total"] else "—") for s in S]))
    rows.append(row("Open &amp; stale &gt; 1 yr", "lower",
                    [(_pct(s["stale365"], s["open_count"]),
                      f"{_pct(s['stale365'], s['open_count']):.0f}%"
                      if s["open_count"] else "—") for s in S]))
    rows.append(row("Open vs. legacy/EOL branch", "lower",
                    [(_pct(s["open_branch_posture"].get("legacy / EOL", 0),
                           s["open_count"]),
                      f"{_pct(s['open_branch_posture'].get('legacy / EOL',0), s['open_count']):.0f}%"
                      if s["open_count"] else "—") for s in S]))
    if any(s.get("rtbc") for s in S):
        rows.append(row("RTBC backlog — median age", "lower",
                        [((s.get("rtbc") or {}).get("median"),
                          human_days((s.get("rtbc") or {}).get("median")))
                         for s in S]))
    if any(s.get("needs_review") for s in S):
        rows.append(row("Needs-review — median age", "lower",
                        [((s.get("needs_review") or {}).get("median"),
                          human_days((s.get("needs_review") or {}).get("median")))
                         for s in S]))
    rows.append(row("Unique issue authors", None,
                    [(s["author_unique"], f"{s['author_unique']:,}")
                     for s in S]))
    rows.append(row("Author concentration (Gini)", "lower",
                    [(s.get("author_gini"),
                      f"{s['author_gini']:.2f}"
                      if s.get("author_gini") is not None else "—")
                     for s in S]))
    rows.append(row("Top-3 author share", "lower",
                    [(s.get("author_top3"),
                      f"{s['author_top3']*100:.0f}%"
                      if s.get("author_top3") is not None else "—")
                     for s in S]))
    rows.append(row("Project age", None,
                    [(None, human_days(_age_days(b["project"])))
                     for b in bundles]))
    if deep_on:
        rows.append(row("Median time to first response", "lower",
                        [((d or {}).get("first_response_median"),
                          human_days((d or {}).get("first_response_median")))
                         for d in D]))
        rows.append(row("Review-state responsiveness", "lower",
                        [((d or {}).get("review_response_median"),
                          human_days((d or {}).get("review_response_median")))
                         for d in D]))
        rows.append(row("Civility index (0–100)", "higher",
                        [(((d or {}).get("tone") or {}).get("civility_index"),
                          f"{((d or {}).get('tone') or {}).get('civility_index'):.0f}"
                          if ((d or {}).get("tone") or {}).get("civility_index")
                          is not None else "—") for d in D]))
    h.append("".join(rows))
    h.append("</tbody></table>")

    # ---------- Queue shape (normalized %) ----------
    h.append("<h2>Queue shape</h2>")
    h.append("<p class='sub'>Distributions as a share of each project's own "
             "issues, so you compare character rather than size.</p>")

    def dist_rows(key, cats):
        out = []
        for c in cats:
            vals = [_pct(S[i][key].get(c, 0), S[i]["total"]) or 0
                    for i in range(cols)]
            out.append((c, vals))
        return out

    h.append("<h3>By priority</h3>")
    h.append(grouped_hbar(
        dist_rows("priority", ["Critical", "Major", "Normal", "Minor",
                               "Unspecified"]), names, colors))
    h.append("<h3>By category</h3>")
    cat_order = ["Bug report", "Task", "Feature request", "Support request",
                 "Plan", "Unspecified"]
    h.append(grouped_hbar(dist_rows("category", cat_order), names, colors))
    h.append("<h3>By core branch</h3>")
    branch_totals = Counter()
    for s in S:
        for k, v in s["branch"].items():
            branch_totals[k] += v
    branch_cats = [k for k, _ in branch_totals.most_common(8)]
    h.append(grouped_hbar(dist_rows("branch", branch_cats), names, colors))
    h.append("<h3>Open vs. resolved</h3>")
    h.append(grouped_hbar(
        [("Open", [_pct(s["open_count"], s["total"]) or 0 for s in S]),
         ("Resolved", [_pct(s["closed_count"], s["total"]) or 0 for s in S])],
        names, colors))

    # ---------- Time-series overlays ----------
    h.append("<h2>Activity over time</h2>")
    h.append("<p class='sub'>Issues opened per year (raw counts).</p>")
    h.append(multi_line(
        [(names[i], colors[i],
          [(y, c) for y, c in sorted(S[i]["created_year"].items())])
         for i in range(cols)], unit="count"))
    if any(s.get("cohort_res") for s in S):
        h.append("<h2>Resolution-time trend</h2>")
        h.append("<p class='sub'>Median days to close, by the year each issue "
                 "was opened (cohorts of 4+ resolved issues).</p>")
        h.append(multi_line(
            [(names[i], colors[i],
              [(yr, med) for yr, (med, _) in
               sorted(S[i].get("cohort_res", {}).items())])
             for i in range(cols)], unit="days"))

    # ---------- Top contributors side by side ----------
    h.append("<h2>Top contributors</h2>")
    h.append("<table><thead><tr><th>Rank</th>"
             + "".join(f"<th>{n}</th>" for n in names)
             + "</tr></thead><tbody>")
    resolved_lists = []
    for i, b in enumerate(bundles):
        top = b["stats"]["author_counts"].most_common(5)
        if opts["resolve"]:
            for uid, _ in top:
                if uid not in b["stats"]["author_names"]:
                    b["stats"]["author_names"][uid] = resolve_username(
                        opts["client"], uid, opts["uname_cache"])
        resolved_lists.append(
            [(b["stats"]["author_names"].get(uid, f"uid:{uid}"), uid, c)
             for uid, c in top])
    for rnk in range(5):
        cells = []
        for lst in resolved_lists:
            if rnk < len(lst):
                name, uid, c = lst[rnk]
                cells.append(f"<td><a href='https://www.drupal.org/user/{uid}'>"
                             f"{escape(name)}</a> "
                             f"<span class='muted'>({c})</span></td>")
            else:
                cells.append("<td>—</td>")
        h.append(f"<tr><td class='muted'>{rnk+1}</td>{''.join(cells)}</tr>")
    h.append("</tbody></table>")

    # ---------- Cross-project contributor overlap ----------
    h.append("<h2>Contributor overlap</h2>")
    h.append("<p class='sub'>How much the projects share the same people — a "
             "map of the maintainer ecosystem around them. Based on issue "
             "authors"
             + (" and commenters" if deep_on else "") + ".</p>")
    author_sets = [set(b["stats"]["author_counts"].keys()) for b in bundles]

    def overlap_table(sets, label):
        rows = ["<table class='score'><thead><tr><th>" + label + "</th>"
                + "".join(f"<th>{n}</th>" for n in names) + "</tr></thead><tbody>"]
        for i in range(cols):
            cells = []
            for j in range(cols):
                if i == j:
                    cells.append(f"<td class='muted'>{len(sets[i]):,}</td>")
                else:
                    inter = len(sets[i] & sets[j])
                    union = len(sets[i] | sets[j]) or 1
                    cells.append(f"<td>{inter:,} "
                                 f"<span class='muted'>({inter/union*100:.0f}% "
                                 f"Jaccard)</span></td>")
            rows.append(f"<tr><td>{names[i]}</td>{''.join(cells)}</tr>")
        rows.append("</tbody></table>")
        return "".join(rows)

    h.append("<h3>Shared issue authors</h3>")
    h.append("<p class='sub'>Diagonal = total unique authors; off-diagonal = "
             "authors in common (with Jaccard overlap).</p>")
    h.append(overlap_table(author_sets, "Authors"))

    # Top shared contributors across ALL projects.
    common = set.intersection(*author_sets) if all(author_sets) else set()
    if common:
        scored = sorted(
            ((sum(b["stats"]["author_counts"].get(uid, 0) for b in bundles),
              uid) for uid in common), reverse=True)[:12]
        if opts["resolve"]:
            for _, uid in scored:
                if uid not in bundles[0]["stats"]["author_names"]:
                    bundles[0]["stats"]["author_names"][uid] = resolve_username(
                        opts["client"], uid, opts["uname_cache"])
        rows = []
        for total_c, uid in scored:
            name = bundles[0]["stats"]["author_names"].get(uid, f"uid:{uid}")
            per = " · ".join(f"{b['stats']['author_counts'].get(uid,0)}"
                             for b in bundles)
            rows.append(f"<tr><td><a href='https://www.drupal.org/user/{uid}'>"
                        f"{escape(name)}</a></td><td class='num'>{total_c}</td>"
                        f"<td class='muted'>{per}</td></tr>")
        h.append(f"<h3>People active across all {cols} projects</h3>")
        h.append(f"<table><thead><tr><th>Contributor</th>"
                 f"<th class='num'>Total issues</th>"
                 f"<th>Per project</th></tr></thead>"
                 f"<tbody>{''.join(rows)}</tbody></table>")
    else:
        h.append("<p class='sub muted'>No single issue author appears across "
                 "all of the compared projects.</p>")

    if deep_on:
        comm_sets = [set((b["deep"] or {}).get("commenter_counts", {}).keys())
                     for b in bundles]
        if all(comm_sets):
            h.append("<h3>Shared commenters (sampled, deep mode)</h3>")
            h.append(overlap_table(comm_sets, "Commenters"))

    # ---------- Raw side-by-side for the fuzzy metrics ----------
    h.append("<h2>Best-effort metrics (raw, side by side)</h2>")
    h.append("<p class='sub'>Not in the scorecard because their accuracy is "
             "limited by what the API exposes — compare with caution.</p>")
    frows = [("Issues with attached files",
              [f"{s['issues_with_files']:,}" for s in S])]
    if deep_on:
        frows.append(("Reopen rate (sampled)",
                      [(f"{(d or {}).get('reopen_issues',0)}/"
                        f"{(d or {}).get('reopen_observable',0)}"
                        if (d or {}).get("reopen_observable") else "n/a")
                       for d in D]))
        frows.append(("Unique commenters (sampled)",
                      [f"{(d or {}).get('commenter_unique','—'):,}"
                       if (d or {}).get("commenter_unique") is not None else "—"
                       for d in D]))
    h.append("<table><thead><tr><th>Metric</th>"
             + "".join(f"<th>{n}</th>" for n in names)
             + "</tr></thead><tbody>")
    for label, vals in frows:
        h.append(f"<tr><td>{label}</td>"
                 + "".join(f"<td class='num'>{v}</td>" for v in vals)
                 + "</tr>")
    h.append("</tbody></table>")

    h.append("<div class='note'><strong>How to read this.</strong> "
             "Distributions are normalized to each project's own total so a "
             "larger queue doesn't dominate. Highlights mark the better value "
             "only for metrics with a clear direction; there is deliberately "
             "no single 'winner', since which metrics matter depends on why "
             "you're comparing. The same methodology caveats as the "
             "single-project report apply (close-time proxy, branch-posture "
             "heuristic, sampled reopen rate).</div>")
    h.append(references_html(D))
    h.append(f"<div class='foot'>Generated {gen} · comparing "
             f"{', '.join(machines)}</div>")
    h.append("</div></body></html>")
    return "".join(h)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Analyze a Drupal.org module issue queue and build an "
                    "HTML report.")
    p.add_argument("project", help="drupal.org project URL or machine name "
                                    "(e.g. https://www.drupal.org/project/lms "
                                    "or just 'lms')")
    p.add_argument("--vs", action="append", default=[], metavar="PROJECT",
                   help="compare against another project (URL or machine "
                        "name). Repeatable: --vs a --vs b for 3-4 projects.")
    p.add_argument("-o", "--out", default=None,
                   help="output HTML path (default: <machine>-report.html)")
    p.add_argument("--deep", action="store_true",
                   help="also fetch comments for the most-discussed issues "
                        "(commenters, time-to-first-response). Slower.")
    p.add_argument("--tone", action="store_true",
                   help="include aggregate tone and civility profile in deep "
                        "mode (requires --deep). Heuristic; see the "
                        "methodology note in the report before relying on it.")
    p.add_argument("--max-comment-issues", type=int, default=150,
                   help="cap on issues fetched in --deep mode (default 150)")
    p.add_argument("--maintainers", default=None, metavar="UIDS",
                   help="comma-separated drupal.org user IDs to treat as "
                        "maintainers, refining maintainer-responsiveness "
                        "(deep mode). Augments any maintainers found on the "
                        "project node.")
    p.add_argument("--no-resolve", action="store_true",
                   help="skip resolving contributor user IDs to usernames")
    p.add_argument("--delay", type=float, default=0.34,
                   help="seconds between API requests (default 0.34)")
    p.add_argument("--no-cache", action="store_true",
                   help="ignore the on-disk response cache")
    p.add_argument("--cache-dir", default=".dorg_cache",
                   help="cache directory (default .dorg_cache)")
    p.add_argument("--open", action="store_true",
                   help="open the report in your browser when done")
    p.add_argument("--pdf", default=None,
                   help="also write a PDF here (requires weasyprint)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    machine = machine_name_from(args.project)
    client = Client(args.cache_dir, delay=args.delay,
                    no_cache=args.no_cache, verbose=args.verbose)
    opts = {"resolve": not args.no_resolve, "client": client,
            "uname_cache": {}, "tag_cache": {}}

    # ---- Comparison mode --------------------------------------------------
    if args.vs:
        machines = [machine] + [machine_name_from(v) for v in args.vs]
        bundles = [collect_project(client, m, args) for m in machines]
        print("› Building comparison report…", file=sys.stderr)
        html = render_comparison(bundles, opts)
        out = args.out or f"{machine}-comparison-report.html"
        with open(out, "w") as f:
            f.write(html)
        print(f"✓ Wrote {out}", file=sys.stderr)
        if args.pdf:
            try:
                from weasyprint import HTML
                HTML(string=html, base_url=".").write_pdf(args.pdf)
                print(f"✓ Wrote {args.pdf}", file=sys.stderr)
            except ImportError:
                print("  (weasyprint not installed — skipping PDF. Use your "
                      "browser's Print → Save as PDF.)", file=sys.stderr)
        if args.open:
            webbrowser.open("file://" + os.path.abspath(out))
        return

    # ---- Single-project mode ---------------------------------------------
    print(f"› Looking up project '{machine}'…", file=sys.stderr)
    project = get_project(client, machine)
    nid = project.get("nid")
    print(f"  found: {project.get('title')} (node {nid})", file=sys.stderr)

    print("› Crawling the issue queue (this can take a while)…",
          file=sys.stderr)
    issues = list(iter_issues(client, nid, verbose=args.verbose))
    print(f"  collected {len(issues):,} issues", file=sys.stderr)

    print("› Crunching statistics…", file=sys.stderr)
    stats = analyze(issues)

    deep = None
    if args.deep and issues:
        print(f"› Deep mode: fetching comments for up to "
              f"{args.max_comment_issues} issues…", file=sys.stderr)
        maint = parse_maintainer_ids(project, args.maintainers)
        deep = deep_comments(client, issues, args.max_comment_issues,
                             verbose=args.verbose, maintainers=maint,
                             tone=args.tone)

    html = render(project, machine, stats, deep, opts)

    out = args.out or f"{machine}-report.html"
    with open(out, "w") as f:
        f.write(html)
    print(f"✓ Wrote {out}", file=sys.stderr)

    if args.pdf:
        try:
            from weasyprint import HTML
            HTML(string=html, base_url=".").write_pdf(args.pdf)
            print(f"✓ Wrote {args.pdf}", file=sys.stderr)
        except ImportError:
            print("  (weasyprint not installed — skipping PDF. "
                  "Open the HTML and use your browser's Print → Save as PDF, "
                  "or `pip install weasyprint`.)", file=sys.stderr)

    if args.open:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
