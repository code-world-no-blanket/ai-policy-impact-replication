"""Anchor dates and pre/post bands shared by the time-windowed collectors.

Bands come from the configured horizons (defaults 180 and 365 days):
pre_long [-365, -180), pre_medium [-180, 0), post_medium [0, 180),
post_long [180, 365), in days relative to the anchor date.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ai_policy_impact.config import AnchorEvent, HorizonConfig

BANDS = ["pre_long", "pre_medium", "post_medium", "post_long"]


def band_bounds(anchor: date, horizons: HorizonConfig) -> dict[str, tuple[date, date]]:
    """Half-open [start, end) date ranges for each band."""
    medium = timedelta(days=horizons.medium_run_days)
    long = timedelta(days=horizons.long_run_days)
    return {
        "pre_long": (anchor - long, anchor - medium),
        "pre_medium": (anchor - medium, anchor),
        "post_medium": (anchor, anchor + medium),
        "post_long": (anchor + medium, anchor + long),
    }


def band_for(day: date, bounds: dict[str, tuple[date, date]]) -> str | None:
    for name, (start, end) in bounds.items():
        if start <= day < end:
            return name
    return None


def band_coverage(bounds: dict, observed_through: date) -> dict[str, int]:
    """Days of each band observed up to and including observed_through."""
    return {
        name: max(0, (min(end, observed_through + timedelta(days=1)) - start).days)
        for name, (start, end) in bounds.items()
    }


def anchor_dates(
    candidate: dict, detected: dict[str, str], event: AnchorEvent
) -> tuple[date | None, str]:
    """The anchor under the configured event; no fallback to the other definition.

    A comparison repository (no AI policy) carries its assigned `stand_in_date`,
    which is used whatever the event.
    """
    if candidate.get("stand_in_date"):
        return date.fromisoformat(candidate["stand_in_date"]), "stand_in"
    if event is AnchorEvent.FIRST_AI_MENTION:
        found = detected.get(candidate["repository"].lower())
        return (date.fromisoformat(found), "detected") if found else (None, "none")
    if candidate.get("reported_policy_date"):
        return date.fromisoformat(candidate["reported_policy_date"]), "reported"
    return None, "none"


def require_policy_run(event: AnchorEvent, policy_run: Path | None) -> None:
    if event is AnchorEvent.FIRST_AI_MENTION and policy_run is None:
        raise ValueError(
            "treatment.anchor_event is first_ai_mention: pass --policy-run "
            "with a policy-history interim run"
        )


def read_detected_dates(policy_run: Path | None) -> dict[str, str]:
    if policy_run is None:
        return {}
    with (policy_run / "policy_events.csv").open(encoding="utf-8") as stream:
        return {
            row["requested_repository"].lower(): row["detected_policy_date"]
            for row in csv.DictReader(stream)
            if row["detected_policy_date"]
        }


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class Window:
    """The observed span around one anchor: all bands, cut at the collection date."""

    bounds: dict[str, tuple[date, date]]
    start: date
    end: date
    coverage: dict[str, int]

    @classmethod
    def around(cls, anchor: date, horizons: HorizonConfig, today: date) -> "Window":
        bounds = band_bounds(anchor, horizons)
        end = min(bounds["post_long"][1] - timedelta(days=1), today)
        return cls(bounds, bounds["pre_long"][0], end, band_coverage(bounds, end))

    @property
    def start_time(self) -> datetime:
        return datetime.combine(self.start, time.min, timezone.utc)

    @property
    def cutoff(self) -> datetime:
        """First instant after the observed window; later events are unobserved."""
        return datetime.combine(self.end + timedelta(days=1), time.min, timezone.utc)

    def band(self, moment: datetime | None) -> str | None:
        """Band of an observed event; None outside the window or after the cutoff."""
        if moment is None or moment >= self.cutoff:
            return None
        return band_for(moment.date(), self.bounds)

    def observed(self, name: str) -> bool:
        return self.coverage[name] > 0
