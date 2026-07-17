from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


QUALITY_LABELS = [
    (9.0, "Excellent"),
    (7.0, "Good"),
    (5.0, "Average"),
    (3.0, "Poor"),
    (0.0, "Very Poor"),
]

class EvaluationResponse(BaseModel):
    model_config = ConfigDict(validate_default=True)

    code_logic: str
    correctness: float = Field(..., ge=0.0, le=10.0)
    code_quality: float = Field(..., ge=0.0, le=10.0)
    efficiency: float = Field(..., ge=0.0, le=10.0)
    overall: str
    common_errors: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    correctness_feedback: str
    improvement_suggestions: list[str] = Field(default_factory=list)
    corrected_code: str

    @field_validator("correctness", "code_quality", "efficiency", mode="before")
    @classmethod
    def _normalize_score(cls, value: Any) -> float:
        return normalize_score(value)

    @field_validator("code_logic", "overall", "correctness_feedback", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("required text fields must not be empty")
        return text

    @field_validator("corrected_code", mode="before")
    @classmethod
    def _corrected_code(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            return "[No corrected code provided by evaluator]"
        return text


    @field_validator("common_errors", mode="before")
    @classmethod
    def _common_errors(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No major errors found"]

    @field_validator("strengths", mode="before")
    @classmethod
    def _strengths(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No specific strengths identified"]

    @field_validator("weaknesses", mode="before")
    @classmethod
    def _weaknesses(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No major weaknesses found"]

    @field_validator("recommendations", mode="before")
    @classmethod
    def _recommendations(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["Review edge cases and keep code readable"]

    @field_validator("improvement_suggestions", mode="before")
    @classmethod
    def _improvement_suggestions(cls, value: Any) -> list[str]:
        return normalize_list(value) or ["No major improvements required"]


def validate_evaluation_response(data: dict[str, Any]) -> EvaluationResponse:
    return EvaluationResponse.model_validate(data)


def evaluation_response_to_dict(model: EvaluationResponse) -> dict[str, Any]:
    return model.model_dump()


def normalize_score(value: Any) -> float:
    if value is None:
        raise ValueError("score is required")

    if isinstance(value, (int, float)):
        return clamp_score(float(value))

    text = str(value).strip()
    if not text:
        raise ValueError("score is empty")

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"could not parse score from {text!r}")

    return clamp_score(float(match.group(0)))


def clamp_score(value: float) -> float:
    return max(0.0, min(10.0, round(float(value), 2)))


def calculate_avg_score(correctness: float, code_quality: float, efficiency: float) -> float:
    return round((clamp_score(correctness) + clamp_score(code_quality) + clamp_score(efficiency)) / 3, 2)


def quality_label(avg_score: float) -> str:
    score = clamp_score(avg_score)
    for lower_bound, label in QUALITY_LABELS:
        if score >= lower_bound:
            return label
    return "Very Poor"


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [text]


class ReviewResponse(BaseModel):
    needs_revision: bool
    reason: str = ""
    code_logic: str = ""
    correctness: float = 0.0
    code_quality: float = 0.0
    efficiency: float = 0.0
    overall: str = ""
    avg_score: float = 0.0
    quality_label: str = ""
    common_errors: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    recommendations: list[str] = []
    correctness_feedback: str = ""
    improvement_suggestions: list[str] = []
    corrected_code: str = ""

