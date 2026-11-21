"""All Pydantic data contracts used by DataPilot."""
import math
import re

from pydantic import BaseModel, Field, field_validator, model_validator

# A unit must appear in at least one axis label of every chart.
_UNIT_WORDS = re.compile(r"₹|%|\bRs\b|\bINR\b|\blakhs?\b|\bcrores?\b|\bunits?\b|\bcount\b|\borders?\b|\bqty\b", re.I)
_LAKH_L = re.compile(r"\bL\b|\bCr\b")  # "₹ L", "Rs Cr" — case-sensitive so a stray "l" doesn't count


def _has_unit(label: str) -> bool:
    return bool(_UNIT_WORDS.search(label) or _LAKH_L.search(label))


class ColumnInfo(BaseModel):
    name: str
    dtype: str
    n_null: int
    sample_values: list[str] = Field(max_length=3)


class DatasetProfile(BaseModel):
    file_name: str
    sheet_name: str | None = None
    n_rows: int
    columns: list[ColumnInfo]
    date_range: tuple[str, str] | None = None


class ChartSpec(BaseModel):
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    x_label: str = Field(min_length=1)
    y_label: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _path_inside_outputs(cls, v: str) -> str:
        p = v.replace("\\", "/")
        if not p.lower().endswith(".png"):
            raise ValueError("chart path must end with .png")
        if not p.startswith("outputs/") or ".." in p.split("/"):
            raise ValueError("chart path must be inside outputs/ (e.g. 'outputs/chart.png')")
        return p

    @model_validator(mode="after")
    def _axis_has_unit(self) -> "ChartSpec":
        if not (_has_unit(self.x_label) or _has_unit(self.y_label)):
            raise ValueError(
                f"chart '{self.title}': at least one axis label needs a unit (₹, Rs, L, %, units, ...)"
            )
        return self


class Finding(BaseModel):
    label: str = Field(min_length=1)
    value: float
    unit: str

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value must be a finite number (no NaN/inf)")
        return v


class AnalysisResult(BaseModel):
    """Contract that analysis.py must write to outputs/result.json."""

    question: str = Field(min_length=1)
    metrics: dict[str, float]
    findings: list[Finding]
    charts: list[ChartSpec] = Field(min_length=1)

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [k for k, x in v.items() if not math.isfinite(x)]
        if bad:
            raise ValueError(f"metrics must be finite numbers (no NaN/inf): {bad}")
        return v


class FinalAnswer(BaseModel):
    answer: str
    where_it_came_from: str
    files: list[str]
    suggested_next_question: str
