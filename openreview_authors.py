"""Extract author OpenReview IDs for every accepted paper across one or more
workshop venues, combining the results into shared authors.csv /
author_scores.csv files.

    uv run python openreview_authors.py ICLR.cc/2025/Workshop/FM-Wild ICML.cc/2024/Workshop/TF2M ...

With no arguments, runs over DEFAULT_VENUES below.

Notes:
  - authorids mixes registered tilde IDs (~First_Last1) with raw email
    addresses for authors who never made a profile. Only the tilde IDs are
    stable usernames; the script separates them.
  - API 2 covers venues from ~2023 onward. Older ones (e.g. DistShift 2022)
    live on API 1 -- the script falls back automatically.
"""

import csv
import getpass
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

import openreview

# OpenReview now requires an authenticated session for API reads (anonymous
# requests get a 403 ChallengeRequiredError). A free account is enough --
# no special access needed for public data. Credentials are resolved in
# order: OPENREVIEW_USERNAME/PASSWORD env vars, then creds.json next to this
# script ({"username": ..., "password": ...}), then an interactive prompt.
CREDS_FILE = Path(__file__).with_name("creds.json")


def load_creds():
    username = os.environ.get("OPENREVIEW_USERNAME")
    password = os.environ.get("OPENREVIEW_PASSWORD")
    if not (username and password) and CREDS_FILE.exists():
        data = json.loads(CREDS_FILE.read_text())
        username = username or data.get("username")
        password = password or data.get("password")
    username = username or input("OpenReview email: ")
    password = password or getpass.getpass("OpenReview password: ")
    return username, password


OR_USERNAME, OR_PASSWORD = load_creds()

# Login once and reuse -- OpenReview rate-limits the /login endpoint itself
# (a handful of logins per short window), separately from normal API calls.
_client2 = None


def api2_client():
    global _client2
    if _client2 is None:
        _client2 = openreview.api.OpenReviewClient(
            baseurl="https://api2.openreview.net", username=OR_USERNAME, password=OR_PASSWORD
        )
    return _client2


def get_notes(venue_id):
    """Try API 2, fall back to the legacy API for pre-2023 venues."""
    notes = api2_client().get_all_notes(content={"venueid": venue_id})
    if notes:
        return notes, 2

    client1 = openreview.Client(
        baseurl="https://api.openreview.net", username=OR_USERNAME, password=OR_PASSWORD
    )
    notes = client1.get_all_notes(invitation=f"{venue_id}/-/Blind_Submission")
    if not notes:
        notes = client1.get_all_notes(invitation=f"{venue_id}/-/Submission")
    return notes, 1


def current_position(profile):
    """Pick the author's current (or most recent) entry from profile history.

    OpenReview lists `history` most-recent-first, but that's a convention,
    not a guarantee, so prefer an entry with no end date (i.e. ongoing);
    fall back to the one with the latest start year; fall back to unknown.
    """
    history = profile.content.get("history") or []
    if not history:
        return "Unknown", "Unknown"

    ongoing = [h for h in history if not h.get("end")]
    entry = ongoing[0] if ongoing else max(history, key=lambda h: h.get("start") or 0)

    position = entry.get("position") or "Unknown"
    institution = (entry.get("institution") or {}).get("name") or "Unknown"
    return position, institution


def classify_career_stage(position):
    """Bucket the free-text position string into a coarse career stage."""
    p = position.lower()
    if "phd" in p or "doctoral" in p:
        return "PhD Student"
    if "postdoc" in p or "post-doc" in p or "post doc" in p:
        return "Postdoc"
    if "assistant professor" in p:
        return "Assistant Professor"
    if "associate professor" in p:
        return "Associate Professor"
    if "professor" in p or "faculty" in p or "lecturer" in p or "reader" in p:
        return "Professor"
    if any(k in p for k in ("master", "msc", "undergrad", "bachelor")) or p.startswith("ms ") or " ms " in p:
        return "Student (Other)"
    if "intern" in p:
        return "Intern"
    if any(k in p for k in ("research scientist", "research engineer", "researcher", "scientist", "engineer")):
        return "Industry/Research"
    if p == "unknown":
        return "Unknown"
    return "Other"


# (name, [domains]) -- matching is by suffix against the primary domain, so
# subdomains (e.g. csail.mit.edu, cs.stanford.edu, mail.utoronto.ca) match
# automatically without listing every variant. Only genuinely separate
# legacy domains (ic.ac.uk, uiuc.edu) need an explicit second entry.
TARGET_UNIVERSITIES = [
    ("MIT", ["mit.edu"]),
    ("Stanford", ["stanford.edu"]),
    ("UC Berkeley", ["berkeley.edu"]),
    ("ETH Zurich", ["ethz.ch"]),
    ("Cambridge", ["cam.ac.uk"]),
    ("Oxford", ["ox.ac.uk"]),
    ("Caltech", ["caltech.edu"]),
    ("Imperial", ["imperial.ac.uk", "ic.ac.uk"]),
    ("Carnegie Mellon", ["cmu.edu"]),
    ("EPFL", ["epfl.ch"]),
    ("Georgia Tech", ["gatech.edu"]),
    ("Michigan", ["umich.edu"]),
    ("UIUC", ["illinois.edu", "uiuc.edu"]),
    ("TU Delft", ["tudelft.nl"]),
    ("TU Munich", ["tum.de"]),
    ("Toronto", ["utoronto.ca"]),
    ("Princeton", ["princeton.edu"]),
    ("Cornell", ["cornell.edu"]),
    ("Harvard", ["harvard.edu"]),
    ("UCLA", ["ucla.edu"]),
    ("UT Austin", ["utexas.edu"]),
    ("UCL", ["ucl.ac.uk"]),
    ("Purdue", ["purdue.edu"]),
    ("Columbia", ["columbia.edu"]),
    ("UC San Diego", ["ucsd.edu"]),
    ("Waterloo", ["uwaterloo.ca"]),
    ("UBC", ["ubc.ca"]),
    ("KU Leuven", ["kuleuven.be"]),
    ("Edinburgh", ["ed.ac.uk"]),
    ("RWTH Aachen", ["rwth-aachen.de"]),
]


def _domain_matches(domain, primary):
    domain = domain.lower()
    primary = primary.lower()
    return domain == primary or domain.endswith("." + primary)


def match_target_universities(emails):
    """emails: iterable of email strings (local part may be masked by
    OpenReview, but the domain is real). Returns sorted matched names."""
    matched = set()
    for email in emails:
        if not email or "@" not in email:
            continue
        domain = email.rsplit("@", 1)[1].strip()
        for name, domains in TARGET_UNIVERSITIES:
            if any(_domain_matches(domain, d) for d in domains):
                matched.add(name)
                break
    return sorted(matched)


def profile_emails(profile):
    """Preferred email + all emails on the profile.

    NOTE: OpenReview masks the local part of every email returned here
    (e.g. "****@cispa.de") unless the requester is the profile owner or
    holds a role (co-author with access, area chair/organizer) that grants
    visibility -- our account has neither for most of these authors, so in
    practice these come back mostly unusable for direct contact. Domain is
    still real and can confirm/cross-check institution.
    """
    preferred = profile.content.get("preferredEmail") or "Unknown"
    all_emails = profile.content.get("emails") or []
    return preferred, ";".join(all_emails)


def fetch_profile_details(tilde_ids):
    """Batch-fetch profiles and return {tilde_id: {...}} with position/
    institution/career_stage/email fields."""
    tilde_ids = list(tilde_ids)
    client = api2_client()
    result = {}

    def extract(profile):
        position, institution = current_position(profile)
        preferred_email, all_emails = profile_emails(profile)
        return {
            "position": position,
            "institution": institution,
            "career_stage": classify_career_stage(position),
            "preferred_email": preferred_email,
            "all_emails": all_emails,
        }

    profiles = client.search_profiles(ids=tilde_ids)
    by_search_id = {p.id: p for p in profiles}
    for tid in tilde_ids:
        if tid in by_search_id:
            result[tid] = extract(by_search_id[tid])

    # search_profiles only exact-matches the id. A paper's authorids can
    # carry an old username that has since been renamed/merged into another
    # profile -- get_profile follows that alias redirect, so retry misses
    # one at a time (there are usually few of these).
    missing = [tid for tid in tilde_ids if tid not in result]
    for tid in missing:
        try:
            profile = client.get_profile(tid)
        except openreview.OpenReviewException:
            profile = None
        if profile:
            result[tid] = extract(profile)

    return result


RELEVANCE_FILE = Path(__file__).with_name("paper_relevance.csv")


def load_paper_relevance():
    """Load per-paper relevance scores keyed by (venue, forum), if the file
    exists. Produced separately (title+abstract judged against a workshop's
    scope) -- see paper_relevance.csv. Returns {} if not generated yet."""
    if not RELEVANCE_FILE.exists():
        return {}
    lookup = {}
    with open(RELEVANCE_FILE, newline="", encoding="utf-8") as fh:
        # Some editor CSV extensions auto-align columns with padding
        # whitespace when the file is open/viewed. That breaks quoting on
        # any rationale that contained a comma (padding gets inserted
        # inside the quotes), so DictReader spills the tail into a `None`
        # key. Rejoin those pieces and strip padding/stray quote chars
        # rather than losing data or re-running the (expensive) scoring.
        for raw_row in csv.DictReader(fh):
            row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items() if k is not None}
            overflow = raw_row.get(None)
            if overflow:
                row["rationale"] = ", ".join([row["rationale"]] + [p.strip() for p in overflow])
            row["rationale"] = row["rationale"].strip(' "')
            lookup[(row["venue"], row["forum"])] = (float(row["relevance"]), row["rationale"])
    return lookup


def field(note, key, api_version):
    """API 2 wraps every content field in {'value': ...}; API 1 does not."""
    raw = note.content.get(key)
    if raw is None:
        return []
    if api_version == 2 and isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, str):
        return [raw]
    return raw or []


def main(venue_ids):
    rows = []
    # tilde id -> {"name", "best_rank", "num_papers", "score", "venues": set()}
    usernames = OrderedDict()
    relevance_lookup = load_paper_relevance()

    for venue_id in venue_ids:
        notes, api_version = get_notes(venue_id)
        print(f"{venue_id}: {len(notes)} papers found (API {api_version})", file=sys.stderr)

        for note in notes:
            title = (field(note, "title", api_version) or ["<untitled>"])[0]
            names = field(note, "authors", api_version)
            ids = field(note, "authorids", api_version)
            num_authors = len(ids)

            for i, author_id in enumerate(ids):
                name = names[i] if i < len(names) else ""
                registered = author_id.startswith("~")
                author_rank = i + 1  # 1 = first author, i.e. most senior credit
                # Fractional/harmonic authorship credit: 1st author = 1.0, 2nd =
                # 0.5, 3rd = 0.33, ... Being buried deep on an 8-author paper
                # barely moves the score; being first author moves it a lot.
                credit = 1 / author_rank
                relevance, rationale = relevance_lookup.get((venue_id, note.forum), (None, ""))
                rows.append(
                    {
                        "venue": venue_id,
                        "paper": title,
                        "forum": note.forum,
                        "author": name,
                        "openreview_id": author_id,
                        "registered": registered,
                        "author_rank": author_rank,
                        "num_authors": num_authors,
                        "credit": round(credit, 4),
                        "paper_relevance": "" if relevance is None else relevance,
                        "paper_relevance_rationale": rationale,
                    }
                )
                if registered:
                    entry = usernames.get(author_id)
                    if entry is None:
                        entry = usernames[author_id] = {
                            "name": name,
                            "best_rank": author_rank,
                            "num_papers": 0,
                            "score": 0.0,
                            "venues": set(),
                            "relevance_weighted_sum": 0.0,
                            "relevance_weight_total": 0.0,
                            "relevance_best": None,
                        }
                    entry["best_rank"] = min(entry["best_rank"], author_rank)
                    entry["num_papers"] += 1
                    entry["score"] += credit
                    entry["venues"].add(venue_id)
                    if relevance is not None:
                        entry["relevance_weighted_sum"] += credit * relevance
                        entry["relevance_weight_total"] += credit
                        entry["relevance_best"] = (
                            relevance
                            if entry["relevance_best"] is None
                            else max(entry["relevance_best"], relevance)
                        )

    rows.sort(key=lambda r: r["credit"], reverse=True)

    with open("authors.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    unregistered = sum(1 for r in rows if not r["registered"])
    print(
        f"{len(rows)} author slots, {len(usernames)} unique tilde IDs, "
        f"{unregistered} unregistered (email only) -> authors.csv",
        file=sys.stderr,
    )

    print(f"Fetching profiles for {len(usernames)} authors...", file=sys.stderr)
    profiles = fetch_profile_details(usernames.keys())
    missing = [t for t in usernames if t not in profiles]
    if missing:
        print(f"{len(missing)} profiles not found (deleted/merged accounts?)", file=sys.stderr)
    if not relevance_lookup:
        print(
            "No paper_relevance.csv found -- relevance columns will be blank "
            "(generate it separately, then rerun)",
            file=sys.stderr,
        )

    empty_profile = {
        "position": "Unknown",
        "institution": "Unknown",
        "career_stage": "Unknown",
        "preferred_email": "Unknown",
        "all_emails": "",
    }

    # Highest score first: rewards both volume (more papers) and placement
    # (earlier author position), same idea as fractional-authorship metrics
    # in bibliometrics. Ties broken by best rank achieved, then name.
    ranked = sorted(
        usernames.items(),
        key=lambda kv: (-kv[1]["score"], kv[1]["best_rank"], kv[1]["name"]),
    )
    fieldnames = [
        "openreview_id",
        "name",
        "num_papers",
        "best_rank",
        "score",
        "num_venues",
        "venues",
        "relevance_weighted_avg",
        "relevance_best_paper",
        "position",
        "institution",
        "career_stage",
        "preferred_email",
        "all_emails",
        "target_universities",
    ]
    score_rows = []
    for tilde, info in ranked:
        profile = profiles.get(tilde, empty_profile)
        venues = sorted(info["venues"])
        weight_total = info["relevance_weight_total"]
        relevance_weighted_avg = (
            round(info["relevance_weighted_sum"] / weight_total, 4) if weight_total else ""
        )
        relevance_best = info["relevance_best"]
        # Local part of every email is masked by OpenReview unless we hold
        # a role granting visibility, but the domain is real -- so this can
        # still match against the target-university list below.
        candidate_emails = [profile["preferred_email"]] + profile["all_emails"].split(";")
        target_universities = match_target_universities(candidate_emails)
        score_rows.append(
            {
                "openreview_id": tilde,
                "name": info["name"],
                "num_papers": info["num_papers"],
                "best_rank": info["best_rank"],
                "score": round(info["score"], 4),
                "num_venues": len(venues),
                "venues": ";".join(venues),
                "relevance_weighted_avg": relevance_weighted_avg,
                "relevance_best_paper": "" if relevance_best is None else relevance_best,
                "position": profile["position"],
                "institution": profile["institution"],
                "career_stage": profile["career_stage"],
                "preferred_email": profile["preferred_email"],
                "all_emails": profile["all_emails"],
                "target_universities": ";".join(target_universities),
            }
        )

    with open("author_scores.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(score_rows)
    print(f"{len(score_rows)} scored authors -> author_scores.csv", file=sys.stderr)

    matched_rows = [r for r in score_rows if r["target_universities"]]
    # Highest best-single-paper relevance first; authors with no relevance
    # data (blank -- no paper_relevance.csv, or no papers matched) sort last.
    matched_rows.sort(
        key=lambda r: r["relevance_best_paper"] if r["relevance_best_paper"] != "" else -1,
        reverse=True,
    )
    with open("author_scores_target_universities.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(matched_rows)
    print(
        f"{len(matched_rows)} authors matched a target-university email domain "
        "-> author_scores_target_universities.csv",
        file=sys.stderr,
    )

    for row in score_rows:
        print(
            f"{row['openreview_id']}\t{row['name']}\tpapers={row['num_papers']}\tscore={row['score']:.3f}"
            f"\t{row['career_stage']} ({row['position']} @ {row['institution']})"
        )


DEFAULT_VENUES = [
    "ICML.cc/2024/Workshop/TF2M",
    "NeurIPS.cc/2025/Workshop/CCFM",
    "NeurIPS.cc/2024/Workshop/Continual_FoMo",
    "NeurIPS.cc/2024/Workshop/AFM",
    "NeurIPS.cc/2022/Workshop/DistShift",
]

if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_VENUES)
