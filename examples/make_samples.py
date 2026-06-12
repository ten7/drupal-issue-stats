#!/usr/bin/env python3
"""
make_samples.py — build illustrative sample reports for the README.

This does NOT touch drupal.org. It synthesizes realistic, lms-shaped issue
data and runs it through dorg_stats.py's *real* analyze() and render()
functions, then injects a visible banner so the output is self-documenting
as sample data. Run `dorg_stats.py lms` on a networked machine for the real
report.
"""
import datetime as dt
import os
import random
import sys
import time

# Import the tool from the repo root (this script lives in examples/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dorg_stats as ds

NOW = ds.NOW
DAY = ds.DAY
random.seed(1107)  # reproducible

YEAR_START = 2014


def ts(year, month=None, day=None):
    month = month or random.randint(1, 12)
    day = day or random.randint(1, 28)
    return int(dt.datetime(year, month, day,
                           random.randint(8, 20), random.randint(0, 59)).timestamp())


# Plausible-but-fictional contributor handles. NOT real drupal.org users.
AUTHORS = [
    (10001, "amelia_dev"), (10002, "drupalcrafter"), (10003, "lms_maint"),
    (10004, "quietfix"), (10005, "node_nomad"), (10006, "patchwork"),
    (10007, "sunny_qa"), (10008, "moduleminder"), (10009, "scope_creep"),
    (10010, "twig_tinker"), (10011, "rtbc_rita"), (10012, "edge_caser"),
    (10013, "a11y_ally"), (10014, "migration_max"), (10015, "cron_carla"),
    (10016, "hooktimer"), (10017, "first_timer_99"), (10018, "drive_by_42"),
    (10019, "lurker_dev"), (10020, "weekend_warrior"),
]

# Heavy-tailed author weighting (a few prolific, long tail).
AUTHOR_WEIGHTS = [40, 34, 60, 22, 18, 16, 14, 13, 11, 10,
                  9, 8, 7, 6, 5, 4, 3, 3, 2, 2]

COMPONENTS = ["Code", "User interface", "Documentation", "Miscellaneous",
              "Migration", "Tests", "Accessibility", "Performance"]
COMP_W = [38, 16, 9, 14, 8, 5, 6, 4]

TAGS = ["Accessibility", "Needs tests", "Needs documentation",
        "Performance", "Security", "D11 compatibility", "Bug Smash Initiative",
        "Novice", "Needs reroll", "Coding standards", "DrupalCon",
        "Needs subsystem maintainer review"]
TAG_W = [10, 14, 9, 6, 4, 12, 5, 7, 8, 6, 3, 2]

# Version targets by era; (version_string, weight)
VERSIONS = [
    ("7.x-1.x-dev", 14), ("7.x-2.x-dev", 8),
    ("8.x-1.x-dev", 20), ("8.x-2.x-dev", 10),
    ("3.0.x-dev", 9), ("4.0.x-dev", 16), ("4.1.x-dev", 11),
    ("10.x", 0), ("11.x", 0),  # placeholders, branch derived from semver
]


def weighted(items, weights):
    return random.choices(items, weights=weights, k=1)[0]


TITLES = [
    "Fatal error on {x} when {y}", "Add support for {x}",
    "{x} not respected in {y}", "Notice: undefined index in {x}",
    "Improve {x} performance on large {y}", "Deprecated {x} in Drupal 10",
    "Document the {x} workflow", "{x} breaks under {y}",
    "Allow configuring {x} per {y}", "Migration path for {x} missing",
    "Accessibility: {x} lacks aria labels", "Cannot save {x} after {y}",
    "Composer install fails with {x}", "Automated tests for {x}",
    "Refactor {x} to use {y}", "{x} should default to {y}",
    "PHP 8.3 compatibility for {x}", "Translation strings in {x} untranslatable",
    "{x} throws warning during cron", "Add a hook for {x}",
]
NOUNS = ["course completion", "the gradebook", "user enrollment", "the catalog",
         "certificate generation", "lesson access", "the activity log",
         "quiz scoring", "the dashboard", "SCORM packages", "the REST resource",
         "the views integration", "badge awarding", "the import form",
         "the settings page", "group membership", "the progress bar",
         "the entity reference field"]


def title():
    t = random.choice(TITLES)
    return t.format(x=random.choice(NOUNS), y=random.choice(NOUNS))


def make_issues(n, semver_share=0.45, resolved_share=0.72, slowdown=1.0,
                base_nid=2000000):
    """Synthesize n lms-shaped issue nodes."""
    issues = []
    # opened-year distribution: ramp up then taper, mature module
    year_weights = {2014: 4, 2015: 7, 2016: 10, 2017: 11, 2018: 12, 2019: 11,
                    2020: 10, 2021: 9, 2022: 9, 2023: 8, 2024: 7, 2025: 6,
                    2026: 2}
    years = list(year_weights)
    yw = list(year_weights.values())
    for i in range(n):
        cyear = random.choices(years, weights=yw, k=1)[0]
        created = ts(cyear)
        # Decide resolved vs open; older issues more likely resolved.
        age_frac = (NOW - created) / (NOW - ts(YEAR_START))
        resolved = random.random() < (resolved_share * (0.55 + 0.45 * age_frac))
        if resolved:
            status = weighted([2, 7, 5, 3, 6, 18],
                              [10, 60, 12, 8, 6, 4])
            # resolution time: median ~ weeks-months, some long tails
            res_days = max(0.2, random.lognormvariate(3.0, 1.25) * slowdown)
            # newer cohorts close faster on average (survivorship)
            res_days *= (1.4 - 0.5 * age_frac)
            changed = min(NOW - DAY, created + int(res_days * DAY))
        else:
            status = weighted([1, 13, 8, 14, 4, 16],
                              [34, 26, 18, 8, 10, 4])
            # last touched: spread, many stale
            touched_ago = random.lognormvariate(4.2, 1.3)
            changed = max(created, NOW - int(touched_ago * DAY))

        # version / branch
        if random.random() < semver_share:
            version = weighted(["3.0.x-dev", "4.0.x-dev", "4.1.x-dev"],
                               [9, 16, 11])
        else:
            version = weighted(["7.x-1.x-dev", "7.x-2.x-dev",
                                "8.x-1.x-dev", "8.x-2.x-dev"],
                               [14, 8, 20, 10])

        aid_idx = random.choices(range(len(AUTHORS)),
                                 weights=AUTHOR_WEIGHTS, k=1)[0]
        aid, aname = AUTHORS[aid_idx]

        # comments: heavy tail
        cc = int(abs(random.lognormvariate(1.4, 1.0)))
        cc = min(cc, 180)

        tags = []
        if random.random() < 0.55:
            for _ in range(random.randint(1, 3)):
                tags.append({"id": None, "name": weighted(TAGS, TAG_W)})

        has_files = random.random() < 0.42
        files = ([{"file": {"filename": f"{random.randint(1,99)}.patch"}}]
                 if has_files else [])

        nid = base_nid + i
        issues.append({
            "nid": nid,
            "title": title(),
            "url": f"https://www.drupal.org/node/{nid}",
            "field_issue_status": status,
            "field_issue_priority": weighted([400, 300, 200, 100],
                                             [3, 14, 70, 13]),
            "field_issue_category": weighted([1, 2, 3, 4, 5],
                                             [46, 18, 22, 12, 2]),
            "field_issue_version": version,
            "field_issue_component": weighted(COMPONENTS, COMP_W),
            "author": {"id": aid, "name": aname},
            "created": str(created),
            "changed": str(changed),
            "comment_count": str(cc),
            "field_issue_tags": tags,
            "field_issue_files": files,
        })
    return issues


SAMPLE_BANNER = (
    "<div style=\"background:#fffbea;border-bottom:2px solid #f6c343;"
    "padding:10px 16px;font:13px/1.5 -apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Roboto,sans-serif;color:#7a5c00;text-align:center\">"
    "<strong>Illustrative sample.</strong> Generated from synthetic, "
    "lms-shaped data to demonstrate the report layout — these are "
    "<strong>not</strong> live Drupal.org figures, and the contributor "
    "names are fictional. Run "
    "<code style=\"background:#fff3cd;padding:1px 5px;border-radius:3px\">"
    "python3 dorg_stats.py lms</code> for the real report.</div>")


def inject_banner(html):
    return html.replace("<body>", "<body>" + SAMPLE_BANNER, 1)


def fake_project(machine, title_, created_year, nid):
    return {"nid": nid, "title": title_,
            "created": str(ts(created_year, 6, 15))}


OPTS = {"resolve": False, "client": None, "uname_cache": {}, "tag_cache": {}}

HERE = os.path.dirname(os.path.abspath(__file__))


def out_path(name):
    return os.path.join(HERE, name)


def main():
    # ---- Single-project: LMS ----
    lms_issues = make_issues(724, semver_share=0.46, resolved_share=0.73,
                             slowdown=1.0, base_nid=2200000)
    lms_proj = fake_project("lms", "LMS", 2014, 2199999)
    lms_stats = ds.analyze(lms_issues)
    html = ds.render(lms_proj, "lms", lms_stats, None, OPTS)
    with open(out_path("lms-report.html"), "w") as f:
        f.write(inject_banner(html))
    print(f"✓ examples/lms-report.html  ({lms_stats['total']} issues, "
          f"{lms_stats['open_count']} open)")

    # ---- Comparison: LMS vs Opigno LMS ----
    opigno_issues = make_issues(468, semver_share=0.30, resolved_share=0.66,
                                slowdown=1.45, base_nid=2400000)
    opigno_proj = fake_project("opigno_lms", "Opigno LMS", 2015, 2399999)
    opigno_stats = ds.analyze(opigno_issues)

    bundles = [
        {"machine": "lms", "project": lms_proj, "stats": lms_stats,
         "deep": None, "issues_n": lms_stats["total"]},
        {"machine": "opigno_lms", "project": opigno_proj,
         "stats": opigno_stats, "deep": None,
         "issues_n": opigno_stats["total"]},
    ]
    chtml = ds.render_comparison(bundles, OPTS)
    with open(out_path("lms-vs-opigno_lms-comparison-report.html"), "w") as f:
        f.write(inject_banner(chtml))
    print(f"✓ examples/lms-vs-opigno_lms-comparison-report.html "
          f"({opigno_stats['total']} issues in opigno_lms)")


if __name__ == "__main__":
    main()
