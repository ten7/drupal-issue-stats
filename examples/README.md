# Sample reports

These files show what Drupal Issue Stats produces, without you having to run a crawl first.

| File | What it is |
|---|---|
| `lms-report.html` / `.pdf` | A single-project report. |
| `lms-vs-opigno_lms-comparison-report.html` / `.pdf` | A two-project comparison. |
| `make_samples.py` | The script that generated the two reports above. |

## These are illustrative, not live

Every file here is built from **synthetic, lms-shaped data** — not a real Drupal.org crawl. We do this so the layout is easy to browse without a network round-trip, and so the contributor names and tone figures are fictional rather than attributed to real people.

The reports run through the tool's *real* `analyze()` and `render()` code, so the structure, charts, and styling are exactly what you'll get. Only the input data is invented. Each sample carries a banner saying so.

To see real numbers, run the tool against a live project:

```bash
python3 dorg_stats.py lms --open
```

## Regenerating the samples

```bash
cd examples
python3 make_samples.py
```

The generator seeds its random number generator, so the output is reproducible run to run.
