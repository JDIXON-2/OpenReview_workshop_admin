"""List authors of every 'Accept (Poster)' paper in the FLLMPT NeurIPS 2026
submission-status export, fetching each paper from its OpenReview forum link.

    uv run python fllmpt_poster_authors.py [input.csv] [output.csv]
"""

import csv
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from openreview_authors import api2_client, field

DEFAULT_INPUT = Path(__file__).with_name("FLLMPT NeurIPS 2026 Submission Status.csv")
DEFAULT_OUTPUT = Path(__file__).with_name("fllmpt_poster_authors.csv")
# Tab-separated, no header: email, first name, last name (mailing-list style).
CONTACTS_OUTPUT = Path(__file__).with_name("fllmpt_poster_first_authors_contacts.tsv")
VENUE_ID = "NeurIPS.cc/2026/Workshop/FLLMPT"
DECISION = "Accept (Poster)"


def main(input_path, output_path):
    # utf-8-sig: the export starts with a BOM that would corrupt the first header.
    with open(input_path, newline="", encoding="utf-8-sig") as fh:
        papers = [r for r in csv.DictReader(fh) if r["decision"].strip() == DECISION]
    print(f"{len(papers)} papers with decision '{DECISION}'", file=sys.stderr)

    client = api2_client()
    rows = []
    for paper in papers:
        forum_id = parse_qs(urlparse(paper["forum"]).query)["id"][0]
        note = client.get_note(forum_id)
        # Newer API 2 venues store authors as dicts ({username, fullname,
        # institutions}); older ones use parallel authors/authorids string lists.
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
        rows.append(
            {
                "number": paper["number"],
                "forum": paper["forum"],
                "title": paper["title"],
                "decision": paper["decision"],
                "first_author": names[0] if names else "",
                "first_author_id": ids[0] if ids else "",
                "first_author_institution": insts[0] if insts else "",
                "num_authors": len(names),
                "authors": "; ".join(names),
                "author_ids": "; ".join(ids),
            }
        )

    # Profile emails are masked (****@domain), but the venue publishes each
    # author's real preferred email as a Preferred_Emails edge (profile id ->
    # email) readable by chairs. Needs a chair account.
    edges = client.get_all_edges(invitation=f"{VENUE_ID}/-/Preferred_Emails")
    emails = {e.head: e.tail for e in edges}
    for r in rows:
        tid = r["first_author_id"]
        r["first_author_profile_url"] = f"https://openreview.net/profile?id={tid}" if tid else ""
        r["first_author_email"] = emails.get(tid, "")
        if not r["first_author_email"]:
            print(f"warning: no preferred email for {tid or r['forum']}", file=sys.stderr)

    # Split names using the profile's own first/last fields -- splitting the
    # full name on spaces breaks on "Yee Whye Teh" / "Gido van de Ven".
    profiles = {
        p.id: p
        for p in client.search_profiles(ids=[r["first_author_id"] for r in rows if r["first_author_id"].startswith("~")])
    }

    def split_name(r):
        profile = profiles.get(r["first_author_id"])
        names = (profile.content.get("names") or []) if profile else []
        if names:
            n = next((n for n in names if n.get("preferred")), names[0])
            if n.get("first") and n.get("last"):
                return f"{n['first']} {n.get('middle') or ''}".replace("  ", " ").strip(), n["last"]
        first, _, last = r["first_author"].partition(" ")
        return first, last

    with open(CONTACTS_OUTPUT, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t")
        seen = set()
        for r in rows:
            email = r["first_author_email"]
            if email and email not in seen:  # one person can first-author several papers
                seen.add(email)
                writer.writerow([email, *split_name(r)])

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} papers -> {output_path}", file=sys.stderr)


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args[0] if args else DEFAULT_INPUT, args[1] if len(args) > 1 else DEFAULT_OUTPUT)
