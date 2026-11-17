"""Typed models for the subset of Personal API responses we use.

Field names follow docs/openapi.json (extracted from the official Swagger docs at
https://developer.garmin.com/gc-developer-program/personal/swagger-docs/).
All models ignore unknown fields; the full raw JSON is always stored in SQLite.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ---------- token ----------
class Token(_Base):
    access_token: str
    expires_at: datetime  # UTC


# ---------- daily summary v2 (/usersummary-service/v2/dailysummaries) ----------
class MovementValue(_Base):
    goal: int | None = None
    value: int | None = None
    distanceInMeters: float | None = None


class Movement(_Base):
    steps: MovementValue | None = None
    floorsAscended: MovementValue | None = None
    floorsDescended: MovementValue | None = None


class Calories(_Base):
    burnedResting: int | None = None
    burnedActive: int | None = None
    burnedTotal: int | None = None


class HeartRate(_Base):
    minValue: int | None = None
    maxValue: int | None = None
    restingValue: int | None = None


class IntensityMinutes(_Base):
    goal: int | None = None
    moderate: int | None = None
    vigorous: int | None = None


class Stress(_Base):
    avgLevel: int | None = None
    maxLevel: int | None = None
    qualifier: str | None = None


class BodyBattery(_Base):
    minValue: int | None = None
    maxValue: int | None = None
    chargedValue: int | None = None
    drainedValue: int | None = None
    endOfDayValue: int | None = None


class Respiration(_Base):
    avgValue: float | None = None
    minValue: float | None = None
    maxValue: float | None = None


class PulseOx(_Base):
    avgValue: float | None = None
    minValue: float | None = None


class DailySummary(_Base):
    calendarDate: date
    movement: Movement | None = None
    calories: Calories | None = None
    heartRate: HeartRate | None = None
    intensityMinutes: IntensityMinutes | None = None
    stress: Stress | None = None
    bodyBattery: BodyBattery | None = None
    respiration: Respiration | None = None
    pulseOx: PulseOx | None = None


# ---------- sleep (/sleep-service/sleep/dailySleepSummariesByDate) ----------
class Sleep(_Base):
    calendarDate: date
    sleepTimeSeconds: int | None = None
    napTimeSeconds: int | None = None
    deepSleepSeconds: int | None = None
    lightSleepSeconds: int | None = None
    remSleepSeconds: int | None = None
    awakeSleepSeconds: int | None = None
    sleepStartTimestampGMT: Any = None
    sleepEndTimestampGMT: Any = None
    averageSpO2Value: float | None = None
    averageRespirationValue: float | None = None
    avgHeartRate: float | None = None
    avgSleepStress: float | None = None
    awakeCount: int | None = None
    sleepScores: dict[str, Any] | None = None

    @property
    def overall_score(self) -> int | None:
        s = self.sleepScores or {}
        overall = s.get("overall") or s.get("overallScore")
        if isinstance(overall, dict):
            return overall.get("value")
        if isinstance(overall, (int, float)):
            return int(overall)
        return None


# ---------- HRV (/hrv-service/hrv/daily/{from}/{to}) ----------
class HrvBaseline(_Base):
    lowUpper: int | None = None
    balancedLow: int | None = None
    balancedUpper: int | None = None
    markerValue: float | None = None


class HrvSummary(_Base):
    calendarDate: date
    weeklyAvg: int | None = None
    lastNightAvg: int | None = None
    lastNight5MinHigh: int | None = None
    baseline: HrvBaseline | None = None
    status: str | None = None


class HrvDaily(_Base):
    hrvSummaries: list[HrvSummary] = Field(default_factory=list)


# ---------- activities (/activitylist-service/activities/search/activities) ----------
class ActivityType(_Base):
    typeId: int | None = None
    typeKey: str | None = None
    parentTypeId: int | None = None


class Activity(_Base):
    activityId: int
    activityName: str | None = None
    activityType: ActivityType | None = None
    startTimeLocal: str | None = None
    startTimeGMT: str | None = None
    duration: float | None = None  # seconds
    movingDuration: float | None = None  # seconds
    elapsedDuration: float | None = None  # seconds
    distance: float | None = None  # meters
    elevationGain: float | None = None  # meters
    averageSpeed: float | None = None  # m/s
    averageHR: float | None = None
    maxHR: float | None = None
    calories: float | None = None
    avgPower: float | None = None
    activityTrainingLoad: float | None = None
    aerobicTrainingEffect: float | None = None
    anaerobicTrainingEffect: float | None = None
    vO2MaxValue: float | None = None
