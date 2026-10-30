"""Borda count for 1st/2nd/3rd ballots. Pure functions, no Discord or DB."""

POINTS = (3, 2, 1)


def tally(ballots, candidate_ids):
    """ballots: iterable of (first, second, third) user ids, None for unused slots.

    Returns (rows, tied) where rows is a list of dicts sorted best-first and
    tied is the list of candidate ids sharing the top place (len 1 = clear winner,
    empty = no votes).
    Tie-break order: points, then 1st-place votes, then 2nd-place votes.
    """
    stats = {c: {"id": c, "points": 0, "firsts": 0, "seconds": 0, "thirds": 0} for c in candidate_ids}
    for ballot in ballots:
        for slot, uid in enumerate(ballot):
            if uid in stats:
                stats[uid]["points"] += POINTS[slot]
                stats[uid][("firsts", "seconds", "thirds")[slot]] += 1
    rows = sorted(stats.values(), key=lambda r: (-r["points"], -r["firsts"], -r["seconds"]))
    if not rows or rows[0]["points"] == 0:
        return rows, []
    key = lambda r: (r["points"], r["firsts"], r["seconds"])
    top = [r["id"] for r in rows if key(r) == key(rows[0])]
    return rows, top
