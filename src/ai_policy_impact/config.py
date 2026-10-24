"""Validated configuration for the research pipeline."""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PanelUnit(StrEnum):
    """Supported units for the primary research panel."""

    REPOSITORY_WEEK = "repository_week"


class ActivityBasis(StrEnum):
    """Information allowed when deciding whether a repository is eligible."""

    PRE_POLICY_ONLY = "pre_policy_only"


class AnchorEvent(StrEnum):
    """Which event dates a repository's policy adoption (the band anchor)."""

    # Earliest commit adding AI wording to a policy path (policy-history run).
    FIRST_AI_MENTION = "first_ai_mention"
    # The registry's transcribed reported_policy_date.
    REPORTED_POLICY_DATE = "reported_policy_date"


class StrictModel(BaseModel):
    """Base model that rejects unknown configuration keys."""

    model_config = ConfigDict(extra="forbid")


class HorizonConfig(StrictModel):
    """Approved post-policy research horizons."""

    medium_run_days: int = Field(default=180, gt=0)
    long_run_days: int = Field(default=365, gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> "HorizonConfig":
        if self.long_run_days <= self.medium_run_days:
            raise ValueError("long_run_days must be greater than medium_run_days")
        return self


class EligibilityConfig(StrictModel):
    """Repository eligibility rules that avoid post-treatment selection."""

    activity_basis: ActivityBasis = ActivityBasis.PRE_POLICY_ONLY
    min_pre_policy_prs: int | None = Field(default=None, ge=1)


class TreatmentConfig(StrictModel):
    """How the policy-adoption date is chosen; provisional until verified."""

    anchor_event: AnchorEvent = AnchorEvent.FIRST_AI_MENTION


class DataPathConfig(StrictModel):
    """Locations for the tracked and local-only data layers."""

    manual: Path = Path("data/repositories")
    raw: Path = Path("data/raw")
    interim: Path = Path("data/interim")
    processed: Path = Path("data")
    state: Path = Path("data/state")


class StudyConfig(StrictModel):
    """Top-level, versioned study configuration."""

    schema_version: Literal[1] = 1
    primary_panel: PanelUnit = PanelUnit.REPOSITORY_WEEK
    horizons: HorizonConfig = Field(default_factory=HorizonConfig)
    eligibility: EligibilityConfig = Field(default_factory=EligibilityConfig)
    treatment: TreatmentConfig = Field(default_factory=TreatmentConfig)
    data: DataPathConfig = Field(default_factory=DataPathConfig)


def load_study_config(path: Path) -> StudyConfig:
    """Load and validate a YAML study configuration without side effects."""

    with path.open("r", encoding="utf-8") as stream:
        payload: Any = yaml.safe_load(stream)

    if payload is None:
        raise ValueError(f"configuration file is empty: {path}")
    if not isinstance(payload, dict):
        raise TypeError(f"configuration root must be a mapping: {path}")

    return StudyConfig.model_validate(payload)
