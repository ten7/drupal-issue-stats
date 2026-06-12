# Drupal Issue Stats

> Point it at a Drupal.org project. Get back a clean HTML report — queue size, resolution speed, contributor concentration, version health, and more — from one command. Compare two or three projects side by side, too.

Built and maintained by [TEN7](https://ten7.com).

---

## Table of Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Usage](#usage)
- [What's in a report](#whats-in-a-report)
- [Comparing projects](#comparing-projects)
- [Deep mode](#deep-mode)
- [Options](#options)
- [Caching and API etiquette](#caching-and-api-etiquette)
- [A note on methodology](#a-note-on-methodology)
- [Project layout](#project-layout)
- [License](#license)
- [About TEN7](#about-ten7)

---

## What it does

A contributed module is only as healthy as its issue queue. We build and rescue Drupal sites for schools and nonprofits, so before we recommend a module — or adopt one into a client's stack — we want to know the things a star count won't tell you. Is anyone home? Is the queue moving or stalled? Does the whole project rest on one person?

Give it a project URL or machine name. It walks the entire issue queue through the public [Drupal.org REST API](https://www.drupal.org/drupalorg/docs/apis/rest-and-other-apis) and produces:

- **A self-contained HTML report** (`<machine>-report.html`) covering open-vs-resolved balance, activity over time, resolution speed by cohort, priority and category mix, Drupal version and support posture, components, top contributors, contribution concentration (a bus-factor read), seasonality, and tag themes.
- **Inline charts** drawn as plain SVG — no JavaScript, no CDN, nothing to load. The file opens anywhere and prints straight to PDF.
- **A comparison report** when you pass `--vs`. Two to four projects land in one scorecard, with distributions normalized so a bigger queue doesn't swamp a smaller one.
- **An optional deep pass** (`--deep`) that reads comments on the most-discussed issues to estimate time-to-first-response, review throughput, and patch-vs-merge-request adoption.

Numbers from a public API carry caveats, and we name them rather than hide them. Every report ends with a methodology note, and the [methodology section below](#a-note-on-methodology) spells out what to trust and what to treat as a rough signal.

---

## How it works

```
lms  ──►  dorg_stats.py  ──►  resolve project (api-d7 node.json)
                         ──►  crawl issue queue, page by page
                         ──►  analyze()           ──►  statistics
                         ──►  [--deep] comments    ──►  engagement + tone
                         ──►  render()  ──►  lms-report.html
                                        ──►  [--pdf] lms-report.pdf
```

1. **Resolve the project.** A URL or bare machine name (`lms`) is looked up against `node.json` to find the project node and its internal ID.
2. **Crawl the queue.** Every `project_issue` node is fetched, one page at a time, with a polite delay and an on-disk cache. Open and closed issues both count.
3. **Analyze.** Counts, medians, distributions, and concentration metrics are computed in memory.
4. **Render.** A single HTML file is written. Add `--pdf` for a PDF, or `--open` to pop it in your browser.

You only ever run `dorg_stats.py`. Everything else happens for you.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.8+** | Verify: `python3 --version` |
| **`requests`** | The one required dependency. Installed via `requirements.txt` below. |
| **`weasyprint`** *(optional)* | For direct `--pdf` output. Without it, open the HTML and use your browser's Print → Save as PDF. |
| **`vaderSentiment`** *(optional)* | Research-grade sentiment for the `--deep` tone profile. Without it, a smaller built-in lexicon fills in. |

---

## Installation

**1. Clone the repository:**

```bash
git clone https://github.com/ten7/drupal-issue-stats.git
cd drupal-issue-stats
```

**2. Create a virtual environment (recommended):**

```bash
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
```

**3. Install dependencies:**

```bash
pip install -r requirements.txt
```

That covers `requests`. To enable PDF export and research-grade sentiment, uncomment the optional lines in `requirements.txt` first, or install them directly:

```bash
pip install weasyprint vaderSentiment
```

---

## Quick start

Analyze a single project:

```bash
python3 dorg_stats.py lms
```

That writes `lms-report.html` in the current folder. Open it, or have the script do it for you:

```bash
python3 dorg_stats.py lms --open
```

A full URL works just as well as a machine name:

```bash
python3 dorg_stats.py https://www.drupal.org/project/lms
```

The first crawl of a large queue takes a few minutes — the tool deliberately paces its requests. The second run is near-instant, because responses are cached.

---

## Usage

```bash
# Single project, open when done.
python3 dorg_stats.py lms --open

# Write to a specific file.
python3 dorg_stats.py lms -o reports/lms.html

# Compare two projects.
python3 dorg_stats.py lms --vs opigno_lms --open

# Compare three or four (repeat --vs).
python3 dorg_stats.py lms --vs opigno_lms --vs h5p

# Deep mode: read comments for richer engagement metrics.
python3 dorg_stats.py lms --deep --open

# Deep mode, naming maintainers for responsiveness stats.
python3 dorg_stats.py lms --deep --maintainers 12345,67890

# Also write a PDF (needs weasyprint).
python3 dorg_stats.py lms --pdf lms-report.pdf
```

---

## What's in a report

See the rendered samples in [`examples/`](examples/):

- [`lms-report.html`](examples/lms-report.html) — a single-project report ([PDF](examples/lms-report.pdf))
- [`lms-vs-opigno_lms-comparison-report.html`](examples/lms-vs-opigno_lms-comparison-report.html) — a side-by-side comparison ([PDF](examples/lms-vs-opigno_lms-comparison-report.pdf))

> **Heads up on the samples.** The files in `examples/` are generated from synthetic, lms-shaped data so the layout is easy to see without a network round-trip. They carry a banner saying so, and the contributor names in them are fictional. Run the tool yourself for real figures.

A single-project report walks top to bottom through:

| Section | What it tells you |
|---|---|
| **Summary cards** | Totals, open vs. resolved, median time to close, median age of open issues, recent activity. |
| **Open vs. resolved** | A donut plus the full status breakdown. |
| **Activity over time** | Issues opened vs. resolved per year. |
| **Resolution-time trend** | Median days to close, grouped by the year an issue was opened — is the project speeding up or slowing down? |
| **Priority & category** | The shape of the work: bugs, tasks, features, support. |
| **Drupal versions** | Core branch mix, exact version targets, and how many open issues sit on legacy/EOL branches. |
| **Contributors** | Most active issue authors, plus a concentration read (Gini, top-3 and top-10 share) as a bus-factor signal. |
| **Seasonality & tags** | When issues get filed, and recurring themes like accessibility or performance. |
| **Queue health snapshot** | Recent touches, stale open issues, issues with attached files. |

---

## Comparing projects

Pass one or more `--vs` flags to put projects head to head:

```bash
python3 dorg_stats.py lms --vs opigno_lms --open
```

The comparison report leads with a **scorecard**. It highlights the better value only where a metric has a clear direction — faster median close time is better, more total issues is just *more*, so volume stays neutral. There's deliberately no single "winner," because which metrics matter depends on why you're comparing.

Below the scorecard, distributions are normalized to each project's own total, so you compare character rather than size. The report also maps **contributor overlap** — how much the projects share the same people — which is often the most revealing part when you're sizing up an ecosystem.

---

## Deep mode

`--deep` fetches comments for the most-discussed issues (capped by `--max-comment-issues`, default 150) and reconstructs per-issue timelines. It adds:

- **Time to first response** and its distribution.
- **Review-state responsiveness** — how fast issues move after they're set to *Needs review* or *RTBC*.
- **Maintainer responsiveness** — pass `--maintainers uid1,uid2` to measure time to a maintainer's first reply.
- **Patch vs. merge-request adoption** over time.

Deep mode is slower, because it makes one request per sampled issue. It's off by default for that reason.

### Tone profile

Add `--tone` alongside `--deep` to also compute an aggregate tone and civility profile — a heuristic read on how discussion in the queue tends to feel:

```bash
python3 dorg_stats.py lms --deep --tone --open
```

**Read it as a rough signal, not a verdict.** It scores the queue *in aggregate, never individuals*. It is English-only, blind to sarcasm and quoted text, and built on lexicons and a sentiment model — not judgment. We included it for community-health framing (in the spirit of [CHAOSS](https://chaoss.community/)), and we'd rather you treat a low score as a prompt to go read the threads yourself than as a conclusion about any person.

---

## Options

| Flag | Default | Description |
|---|---|---|
| `project` | — | Project URL or machine name (required). |
| `--vs PROJECT` | — | Compare against another project. Repeatable for 3–4 total. |
| `-o`, `--out PATH` | `<machine>-report.html` | Output HTML path. |
| `--deep` | off | Read comments for engagement metrics (response times, patch/MR adoption). Slower. |
| `--tone` | off | Add aggregate tone and civility profile. Requires `--deep`. Heuristic — read the methodology note before relying on it. |
| `--max-comment-issues N` | `150` | Cap on issues fetched in deep mode. |
| `--maintainers UIDS` | — | Comma-separated Drupal.org user IDs to treat as maintainers. |
| `--no-resolve` | off | Skip resolving contributor IDs to usernames (faster, fewer requests). |
| `--delay SECONDS` | `0.34` | Seconds between API requests. |
| `--no-cache` | off | Ignore the on-disk response cache. |
| `--cache-dir DIR` | `.dorg_cache` | Where cached responses live. |
| `--open` | off | Open the report in your browser when done. |
| `--pdf PATH` | — | Also write a PDF (needs `weasyprint`). |
| `-v`, `--verbose` | off | Print progress as it crawls. |

---

## Caching and API etiquette

This tool reads from a **public, shared API** that other people depend on. It behaves accordingly:

- It **paces itself** — roughly three requests per second by default (`--delay`). Raise the delay if you want to be gentler.
- It **caches every response** to `.dorg_cache/`, so re-running a report costs no new requests. The cache is gitignored, since it holds fetched usernames and comment text. Delete the folder to start fresh, or pass `--no-cache`.
- It **retries with backoff** on transient errors and identifies itself with a descriptive User-Agent.

Please keep it that way. If you fork this and crank the delay to zero, you're being rude to the whole Drupal community.

---

## A note on methodology

The data is public and so are its limits. We'd rather you know them:

- **"Time to close" is a proxy.** It uses each issue's last-modified timestamp as a stand-in for its close date, so a late edit to an old closed issue can inflate it.
- **The resolution-time trend favors recent years.** Recent cohorts only contain the issues that closed quickly enough to already be closed, so they can look artificially fast.
- **Support posture is a heuristic.** Current-vs-legacy is inferred from the branch number; it doesn't read a project's own supported-branch declarations.
- **Deep-mode timeline metrics depend on what the API exposes.** Where comment data doesn't carry the needed status snapshots, a metric degrades to empty rather than guessing.

Every report repeats the relevant caveats inline, next to the numbers they apply to.

---

## Project layout

| File | Role |
|---|---|
| `dorg_stats.py` | The whole tool. Crawls, analyzes, and renders the report. |
| `requirements.txt` | Dependencies — `requests` required, `weasyprint` and `vaderSentiment` optional. |
| `examples/` | Rendered sample reports plus the script that builds them (synthetic data, for layout reference). |
| `CHANGELOG.md` | Notable changes by version. |
| `LICENSE` | GPLv3 license text. |
| `.dorg_cache/` | Cached API responses (gitignored, created on first run). |

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPLv3)**. See the [LICENSE](LICENSE) file for details.

---

## About TEN7

We built and maintain this tool at [TEN7](https://ten7.com), a digital agency that builds, rescues, and cares for Drupal sites. Our mission is to **Make Things That Matter**.

- 🌐 [ten7.com](https://ten7.com)
- 📖 [handbook.ten7.com](https://handbook.ten7.com)
