# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-06-12

Initial public release.

### Added
- Single-project issue-queue report with inline SVG charts (no JS, no CDN).
- Open-vs-resolved balance, activity-over-time, and resolution-time trend by cohort.
- Priority, category, Drupal version, support-posture, and component breakdowns.
- Contributor activity and concentration metrics (Gini, top-3/top-10 share).
- Seasonality and tag-theme summaries.
- Side-by-side comparison report via `--vs` (2–4 projects) with a scorecard and contributor-overlap map.
- Optional deep mode (`--deep`) for time-to-first-response, review-state responsiveness, maintainer responsiveness, and patch-vs-merge-request adoption.
- Optional tone and civility profile via `--tone` (requires `--deep`). Off by default; heuristic — see the methodology note in the report.
- On-disk response caching, polite request pacing, and retry-with-backoff.
- Optional PDF output via WeasyPrint (`--pdf`); otherwise print the HTML from a browser.
- GPLv3 license.
