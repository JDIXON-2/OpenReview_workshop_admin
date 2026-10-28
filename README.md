# OpenReview workshop admin

Scripts for pulling author/paper data from OpenReview for workshop administration.

## Setup

```sh
uv sync
```

Credentials are read from `OPENREVIEW_USERNAME` / `OPENREVIEW_PASSWORD`, then
`creds.json` (`{"username": ..., "password": ...}`) next to the scripts, then an
interactive prompt. `creds.json` and all data files (`*.csv`, `*.tsv`, `*.json`)
are git-ignored.

## Scripts

- `openreview_authors.py` — shared OpenReview client helpers and author scraping.
- `fllmpt_oral_authors.py [input.csv] [output.csv]` — authors (with emails) of "Accept (Oral)" papers.
- `fllmpt_poster_authors.py [input.csv] [output.csv]` — authors of "Accept (Poster)" papers, plus a first-author contacts TSV.
