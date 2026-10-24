"""Combine reused and newly collected matches into the matched comparison sample.
Kept as a record: it reads working files (logs/eval/...) from the original run.

usage: python combine_matched.py <out.csv> [batch numbers to leave out]
"""
import csv, glob, re, sys
out, leave = sys.argv[1], set(sys.argv[2:])
cols = ["repository", "priority", "policy_paths", "group", "stand_in_date", "matched_policy_repo", "source"]
rows = [dict(r, source="original comparison set") for r in csv.DictReader(open("logs/eval/matched-existing.csv"))]
for path in sorted(glob.glob("logs/eval/matched-batch-*.csv"), key=lambda p: int(re.findall(r"\d+", p)[-1])):
    if re.findall(r"\d+", path)[-1] in leave:
        continue
    rows += [dict(r, source="matched") for r in csv.DictReader(open(path))]
assert len({r["repository"].lower() for r in rows}) == len(rows)
assert len({r["matched_policy_repo"] for r in rows}) == len(rows)
with open(out, "w", newline="") as s:
    w = csv.DictWriter(s, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
print(len(rows), "->", out)
