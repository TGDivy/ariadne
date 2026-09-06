"""Typed request and response models for Ithaca's health read APIs."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class APIModel(BaseModel):
    """Strict finite JSON at the Ariadne/Ithaca boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WorkoutActivityType(StrEnum):
    """Readable HealthKit workout filters accepted by Ithaca read v1."""

    AMERICAN_FOOTBALL = "american_football"
    ARCHERY = "archery"
    AUSTRALIAN_FOOTBALL = "australian_football"
    BADMINTON = "badminton"
    BASEBALL = "baseball"
    BASKETBALL = "basketball"
    BOWLING = "bowling"
    BOXING = "boxing"
    CLIMBING = "climbing"
    CRICKET = "cricket"
    CROSS_TRAINING = "cross_training"
    CURLING = "curling"
    CYCLING = "cycling"
    DANCE = "dance"
    ELLIPTICAL = "elliptical"
    EQUESTRIAN_SPORTS = "equestrian_sports"
    FENCING = "fencing"
    FISHING = "fishing"
    FUNCTIONAL_STRENGTH_TRAINING = "functional_strength_training"
    GOLF = "golf"
    GYMNASTICS = "gymnastics"
    HANDBALL = "handball"
    HIKING = "hiking"
    HOCKEY = "hockey"
    HUNTING = "hunting"
    LACROSSE = "lacrosse"
    MARTIAL_ARTS = "martial_arts"
    MIND_AND_BODY = "mind_and_body"
    PADDLE_SPORTS = "paddle_sports"
    PLAY = "play"
    PREPARATION_AND_RECOVERY = "preparation_and_recovery"
    RACQUETBALL = "racquetball"
    ROWING = "rowing"
    RUGBY = "rugby"
    RUNNING = "running"
    SAILING = "sailing"
    SKATING_SPORTS = "skating_sports"
    SNOW_SPORTS = "snow_sports"
    SOCCER = "soccer"
    SOFTBALL = "softball"
    SQUASH = "squash"
    STAIR_CLIMBING = "stair_climbing"
    SURFING_SPORTS = "surfing_sports"
    SWIMMING = "swimming"
    TABLE_TENNIS = "table_tennis"
    TENNIS = "tennis"
    TRACK_AND_FIELD = "track_and_field"
    TRADITIONAL_STRENGTH_TRAINING = "traditional_strength_training"
    VOLLEYBALL = "volleyball"
    WALKING = "walking"
    WATER_FITNESS = "water_fitness"
    WATER_POLO = "water_polo"
    WATER_SPORTS = "water_sports"
    WRESTLING = "wrestling"
    YOGA = "yoga"
    BARRE = "barre"
    CORE_TRAINING = "core_training"
    CROSS_COUNTRY_SKIING = "cross_country_skiing"
    DOWNHILL_SKIING = "downhill_skiing"
    FLEXIBILITY = "flexibility"
    HIGH_INTENSITY_INTERVAL_TRAINING = "high_intensity_interval_training"
    JUMP_ROPE = "jump_rope"
    KICKBOXING = "kickboxing"
    PILATES = "pilates"
    SNOWBOARDING = "snowboarding"
    STAIRS = "stairs"
    STEP_TRAINING = "step_training"
    WHEELCHAIR_WALK_PACE = "wheelchair_walk_pace"
    WHEELCHAIR_RUN_PACE = "wheelchair_run_pace"
    TAI_CHI = "tai_chi"
    MIXED_CARDIO = "mixed_cardio"
    HAND_CYCLING = "hand_cycling"
    DISC_SPORTS = "disc_sports"
    FITNESS_GAMING = "fitness_gaming"
    CARDIO_DANCE = "cardio_dance"
    SOCIAL_DANCE = "social_dance"
    PICKLEBALL = "pickleball"
    COOLDOWN = "cooldown"
    SWIM_BIKE_RUN = "swim_bike_run"
    TRANSITION = "transition"
    UNDERWATER_DIVING = "underwater_diving"
    OTHER = "other"


class WorkoutPeriodRequest(APIModel):
    start_at: AwareDatetime
    end_at: AwareDatetime
    activity_types: list[WorkoutActivityType] = Field(
        default_factory=list, max_length=32
    )

    @model_validator(mode="after")
    def require_forward_utc_period(self) -> WorkoutPeriodRequest:
        if self.start_at.utcoffset() != timedelta(
            0
        ) or self.end_at.utcoffset() != timedelta(0):
            raise ValueError("start_at and end_at must use UTC")
        if self.end_at <= self.start_at:
            raise ValueError("end must be later than start")
        return self


class WorkoutSearchRequest(WorkoutPeriodRequest):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)


class WorkoutSummarizeRequest(WorkoutPeriodRequest):
    pass


class WorkoutShowRequest(APIModel):
    workout_uuid: UUID
    snapshot_id: UUID | None = None


class Activity(APIModel):
    code: int = Field(ge=0)
    name: str


class Source(APIModel):
    name: str


class Selection(APIModel):
    snapshot_id: UUID
    newer_unqueryable_snapshot_exists: bool


class PeriodProjectionCoverage(APIModel):
    scope: Literal["requested_period_before_activity_filters"]
    canonical_workout_count: int = Field(ge=0)
    queryable_workout_count: int = Field(ge=0)
    unqueryable_workout_count: int = Field(ge=0)
    workouts_with_newer_unqueryable_snapshot_count: int = Field(ge=0)


class WorkoutSearchItem(APIModel):
    workout_uuid: UUID
    snapshot_id: UUID
    newer_unqueryable_snapshot_exists: bool
    activity: Activity
    start_at: datetime
    end_at: datetime
    source: Source
    workout_time_seconds: float = Field(ge=0)
    elapsed_time_seconds: float = Field(ge=0)
    distance_meters: float | None = Field(default=None, ge=0)
    average_pace_seconds_per_kilometer: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=0)
    route_available: bool
    quality_issue_count: int = Field(ge=0)


class WorkoutSearchResponse(APIModel):
    schema_version: Literal[1]
    projection_coverage: PeriodProjectionCoverage
    items: list[WorkoutSearchItem]
    next_cursor: str | None


class WorkoutSummaryTotals(APIModel):
    workout_count: int = Field(ge=0)
    workout_time_seconds: float = Field(ge=0)
    elapsed_time_seconds: float = Field(ge=0)


class WorkoutActivityAggregate(APIModel):
    activity: Activity
    workout_count: int = Field(ge=0)
    workout_time_seconds: float = Field(ge=0)
    elapsed_time_seconds: float = Field(ge=0)
    distance_meters: float | None = Field(default=None, ge=0)
    active_energy_kilocalories: float | None = Field(default=None, ge=0)
    elevation_gain_meters: float | None = Field(default=None, ge=0)
    step_count: float | None = Field(default=None, ge=0)
    average_pace_seconds_per_kilometer: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=0)
    average_power_watts: float | None = Field(default=None, ge=0)


class WorkoutSummarizeResponse(APIModel):
    schema_version: Literal[1]
    start_at: datetime
    end_at: datetime
    projection_coverage: PeriodProjectionCoverage
    totals: WorkoutSummaryTotals
    activities: list[WorkoutActivityAggregate]


class WorkoutMetrics(APIModel):
    workout_time_seconds: float = Field(ge=0)
    elapsed_time_seconds: float = Field(ge=0)
    distance_meters: float | None = Field(default=None, ge=0)
    active_energy_kilocalories: float | None = Field(default=None, ge=0)
    elevation_gain_meters: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=0)
    average_power_watts: float | None = Field(default=None, ge=0)
    average_running_cadence_steps_per_minute: float | None = Field(default=None, ge=0)
    average_cycling_cadence_revolutions_per_minute: float | None = Field(
        default=None, ge=0
    )
    average_pace_seconds_per_kilometer: float | None = Field(default=None, ge=0)
    step_count: float | None = Field(default=None, ge=0)
    effort_score: float | None = None


class RunningDynamics(APIModel):
    average_ground_contact_time_milliseconds: float | None = Field(default=None, ge=0)
    average_stride_length_meters: float | None = Field(default=None, ge=0)
    average_vertical_oscillation_centimeters: float | None = Field(default=None, ge=0)


class ActivityComponentMetrics(APIModel):
    distance_meters: float | None = Field(default=None, ge=0)
    active_energy_kilocalories: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=0)
    average_power_watts: float | None = Field(default=None, ge=0)
    average_running_cadence_steps_per_minute: float | None = Field(default=None, ge=0)
    average_cycling_cadence_revolutions_per_minute: float | None = Field(
        default=None, ge=0
    )
    average_pace_seconds_per_kilometer: float | None = Field(default=None, ge=0)
    step_count: float | None = Field(default=None, ge=0)


class ActivityComponent(APIModel):
    activity_uuid: UUID
    activity: Activity
    start_at: datetime
    end_at: datetime | None
    workout_time_seconds: float = Field(ge=0)
    elapsed_time_seconds: float | None = Field(default=None, ge=0)
    metrics: ActivityComponentMetrics


class ActivityComponents(APIModel):
    availability: Literal["available", "not_recorded", "not_applicable", "unavailable"]
    unavailable_reason: str | None = None
    items: list[ActivityComponent]


class DistanceSplit(APIModel):
    index: int = Field(ge=1)
    start_offset_seconds: float = Field(ge=0)
    end_offset_seconds: float = Field(ge=0)
    workout_time_seconds: float | None = Field(default=None, ge=0)
    distance_meters: float | None = Field(default=None, ge=0)
    average_pace_seconds_per_kilometer: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=0)
    heart_rate_coverage_fraction: float | None = Field(default=None, ge=0, le=1)
    average_power_watts: float | None = Field(default=None, ge=0)
    power_coverage_fraction: float | None = Field(default=None, ge=0, le=1)


class DistanceSplits(APIModel):
    availability: Literal[
        "available", "partial", "not_recorded", "not_applicable", "unavailable"
    ]
    unavailable_reason: str | None = None
    target_distance_meters: float | None = Field(default=None, ge=0)
    items: list[DistanceSplit]


class HeartRateZoneDurations(APIModel):
    zone_1: float = Field(ge=0)
    zone_2: float = Field(ge=0)
    zone_3: float = Field(ge=0)
    zone_4: float = Field(ge=0)
    zone_5: float = Field(ge=0)


class HeartRateZones(APIModel):
    availability: Literal["available", "partial", "not_recorded", "unavailable"]
    unavailable_reason: str | None = None
    profile_id: str | None = None
    resting_heart_rate_bpm: int | None = Field(default=None, ge=0)
    maximum_heart_rate_bpm: int | None = Field(default=None, ge=0)
    lower_boundaries_bpm: list[int]
    eligible_duration_seconds: float | None = Field(default=None, ge=0)
    covered_duration_seconds: float | None = Field(default=None, ge=0)
    coverage_fraction: float | None = Field(default=None, ge=0, le=1)
    duration_seconds_by_zone: HeartRateZoneDurations | None


class Route(APIModel):
    availability: Literal["available", "not_recorded", "unavailable"]
    route_count: int = Field(ge=0)
    point_count: int = Field(ge=0)


class Diving(APIModel):
    availability: Literal["available", "partial"]
    maximum_depth_meters: float | None = Field(default=None, ge=0)
    average_water_temperature_celsius: float | None = None
    dive_count: int | None = Field(default=None, ge=0)
    underwater_time_seconds: float | None = Field(default=None, ge=0)


class WorkoutQuality(APIModel):
    capture_complete: bool
    issue_codes: list[str]


class Workout(APIModel):
    workout_uuid: UUID
    activity: Activity
    start_at: datetime
    end_at: datetime
    source: Source
    metrics: WorkoutMetrics
    running_dynamics: RunningDynamics | None
    activity_components: ActivityComponents
    distance_splits: DistanceSplits
    heart_rate_zones: HeartRateZones
    route: Route
    diving: Diving | None
    available_series: list[str]
    quality: WorkoutQuality


class WorkoutShowResponse(APIModel):
    schema_version: Literal[1]
    selection: Selection
    workout: Workout


class SleepDateRangeRequest(APIModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def require_forward_date_range(self) -> SleepDateRangeRequest:
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be later than start_date")
        return self


class SleepSearchRequest(SleepDateRangeRequest):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)


class SleepSummarizeRequest(SleepDateRangeRequest):
    pass


class SleepShowRequest(APIModel):
    sleep_date: date


class SleepProjection(APIModel):
    schema_version: Literal[2]
    stale: bool


class SleepStageDurations(APIModel):
    asleep_unspecified_seconds: float | None = Field(default=None, ge=0)
    core_seconds: float | None = Field(default=None, ge=0)
    deep_seconds: float | None = Field(default=None, ge=0)
    rem_seconds: float | None = Field(default=None, ge=0)


class MainSleepSummary(APIModel):
    sleep_started_at: AwareDatetime
    sleep_ended_at: AwareDatetime
    sleep_window_seconds: float = Field(ge=0)
    total_asleep_seconds: float = Field(ge=0)
    awake_seconds: float | None = Field(default=None, ge=0)
    stages: SleepStageDurations


class AdditionalSleepSummary(APIModel):
    episode_count: int = Field(ge=0)
    total_asleep_seconds: float = Field(ge=0)


class SleepDaySummary(APIModel):
    sleep_date: date
    time_zone_identifier: str
    source_names: list[str]
    main_sleep: MainSleepSummary
    additional_sleep: AdditionalSleepSummary
    total_asleep_seconds: float = Field(ge=0)
    issue_codes: list[str]


class SleepSearchResponse(APIModel):
    schema_version: Literal[1]
    projection: SleepProjection
    sleep_days: list[SleepDaySummary]
    next_cursor: str | None


class SleepStageInterval(APIModel):
    stage: Literal["awake", "asleep_unspecified", "core", "deep", "rem"]
    start_at: AwareDatetime
    end_at: AwareDatetime


class SleepEpisodeDetail(APIModel):
    sleep_started_at: AwareDatetime
    sleep_ended_at: AwareDatetime
    time_zone_identifier: str
    source_names: list[str]
    sleep_window_seconds: float = Field(ge=0)
    total_asleep_seconds: float = Field(ge=0)
    awake_seconds: float | None = Field(default=None, ge=0)
    stages: SleepStageDurations
    stage_intervals: list[SleepStageInterval]
    issue_codes: list[str]


class SleepDayDetail(APIModel):
    sleep_date: date
    time_zone_identifier: str
    source_names: list[str]
    total_asleep_seconds: float = Field(ge=0)
    main_sleep: SleepEpisodeDetail
    additional_sleep: list[SleepEpisodeDetail]
    issue_codes: list[str]


class SleepShowResponse(APIModel):
    schema_version: Literal[1]
    projection: SleepProjection
    sleep_day: SleepDayDetail


class SleepLatestResponse(APIModel):
    schema_version: Literal[1]
    projection: SleepProjection
    sleep_day: SleepDayDetail | None


class AverageLocalTime(APIModel):
    local_time: time
    day_offset_from_sleep_date: int


class SleepSummaryAverages(APIModel):
    main_asleep_seconds: float = Field(ge=0)
    total_asleep_seconds: float = Field(ge=0)
    main_sleep_start: AverageLocalTime
    main_sleep_end: AverageLocalTime


class SleepSummaryVariability(APIModel):
    main_asleep_standard_deviation_seconds: float = Field(ge=0)
    total_asleep_standard_deviation_seconds: float = Field(ge=0)
    main_sleep_start_standard_deviation_seconds: float = Field(ge=0)
    main_sleep_end_standard_deviation_seconds: float = Field(ge=0)


class SleepSummaryAdditionalSleep(APIModel):
    episode_count: int = Field(ge=0)
    total_asleep_seconds: float = Field(ge=0)
    average_asleep_seconds_per_sleep_day: float | None = Field(default=None, ge=0)


class SleepSummarizeResponse(APIModel):
    schema_version: Literal[1]
    start_date: date
    end_date: date
    projection: SleepProjection
    days_with_sleep_data: int = Field(ge=0)
    averages: SleepSummaryAverages | None
    variability: SleepSummaryVariability | None
    additional_sleep: SleepSummaryAdditionalSleep
