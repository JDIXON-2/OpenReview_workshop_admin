"""List every author (with email) of every 'Accept (Oral)' paper in the FLLMPT
NeurIPS 2026 submission-status export.

    uv run python fllmpt_oral_authors.py [input.csv] [output.csv]

Output is one row per (paper, author).
"""

import csv
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from openreview_authors import api2_client, field

DEFAULT_INPUT = Path(__file__).with_name("FLLMPT NeurIPS 2026 Submission Status.csv")
DEFAULT_OUTPUT = Path(__file__).with_name("fllmpt_oral_authors.csv")
VENUE_ID = "NeurIPS.cc/2026/Workshop/FLLMPT"
DECISION = "Accept (Oral)"


def main(input_path, output_path):
    # utf-8-sig: the export starts with a BOM that would corrupt the first header.
    with open(input_path, newline="", encoding="utf-8-sig") as fh:
        papers = [r for r in csv.DictReader(fh) if r["decision"].strip() == DECISION]
    print(f"{len(papers)} papers with decision '{DECISION}'", file=sys.stderr)

    client = api2_client()
    # Profile emails are masked (****@domain); the venue publishes each author's
    # real preferred email as a Preferred_Emails edge (profile id -> email).
    # Needs a chair account.
    emails = {e.head: e.tail for e in client.get_all_edges(invitation=f"{VENUE_ID}/-/Preferred_Emails")}

    rows = []
    for paper in papers:
        forum_id = parse_qs(urlparse(paper["forum"]).query)["id"][0]
        note = client.get_note(forum_id)
        authors = field(note, "authors", 2)
        if authors and isinstance(authors[0], dict):
            names = [a.get("fullname", "") for a in authors]
            ids = [a.get("username", "") for a in authors]
            insts = [(a.get("institutions") or [{}])[0].get("name", "") for a in authors]
        else:
            names, ids = authors, field(note, "authorids", 2)
            insts = [""] * len(names)
        if not names:
            print(f"warning: no authors visible for {paper['forum']}", file=sys.stderr)
        for i, (name, aid) in enumerate(zip(names, ids), start=1):
            # Authors without a profile are listed by raw email address.
            email = emails.get(aid) or (aid if "@" in aid and not aid.startswith("~") else "")
            if not email:
                print(f"warning: no email for {aid} ({paper['title'][:40]})", file=sys.stderr)
            rows.append(
                {
                    "number": paper["number"],
                    "forum": paper["forum"],
                    "title": paper["title"],
                    "author_rank": i,
                    "author": name,
                    "author_id": aid,
                    "institution": insts[i - 1],
                    "email": email,
                }
            )

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(papers)} papers, {len(rows)} author rows -> {output_path}", file=sys.stderr)


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args[0] if args else DEFAULT_INPUT, args[1] if len(args) > 1 else DEFAULT_OUTPUT)
