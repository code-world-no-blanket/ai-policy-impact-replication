"""Characteristics at the reference date for Table I (policy types vs matched comparisons).

usage: python sample_table.py <weekly panel dir>
"""
import sys
import pandas as pd
P = sys.argv[1]
c = pd.read_csv(f"{P}/characteristics.csv")
p = pd.read_csv(f"{P}/panel.csv", usecols=["repository", "event_week", "prs_opened", "active_contributors"])
pre = p[p.event_week.between(-52, -1)].groupby("repository").agg(
    prs_year=("prs_opened", "sum"), active=("active_contributors", "mean"))
post = p[p.event_week >= 0].groupby("repository").event_week.max() + 1
c = c.join(pre, on="repository").join(post.rename("post_weeks"), on="repository")
c["col"] = c.policy_type.where(c.group == "policy", "comparison")
order = ["banned", "restrictive", "permissive", "all", "comparison"]
rows = {}
for col in order:
    d = c[c.group == "policy"] if col == "all" else c[c.col == col]
    rows[col] = {
        "Repositories": len(d),
        "Median stars": f"{d.stars_at_policy.median():,.0f}",
        "Median forks": f"{d.forks_at_policy.median():,.0f}",
        "Median PRs, prior yr": f"{d.prs_year.median():,.0f}",
        "Mean active contr./wk": f"{d.active.median():.1f}",
        "Org.-owned": f"{(d.owner_type == 'Organization').mean():.0%}",
        "Median age (yrs)": f"{d.age_at_policy_years.median():.1f}",
        "Median ref. date": pd.to_datetime(d.anchor_date).median().strftime("%Y-%m"),
        "Post weeks, median": f"{d.post_weeks.median():.0f}",
    }
print(pd.DataFrame(rows).to_string())
