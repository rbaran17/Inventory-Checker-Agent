"""Domain model for physical inventory observation (PRD: "Structured observation data model").

Every observation is a typed record linked to one or more evidence items. Records are
versioned: a correction adds a new version with a reason and the old one stays readable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class SessionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    REVIEWED = "reviewed"


class RecordType(str, Enum):
    TEST_COUNT = "test_count"
    PROCEDURE = "procedure_observation"
    CONDITION = "condition_note"
    CUTOFF = "cutoff_document"
    TAG_CONTROL = "tag_control"


class FieldSource(str, Enum):
    """How a value got into a record. Guards against over-trust in AI suggestions."""

    TYPED = "typed"
    ACCEPTED = "accepted"  # AI suggestion accepted unchanged
    EDITED = "edited"  # AI suggestion edited by the observer
    IMPORTED = "imported"  # e.g. client count from the perpetual listing


class ReviewAction(str, Enum):
    APPROVE = "approve"
    QUERY = "query"
    REJECT = "reject"
    NOTE = "note"  # follow-up note; the only change allowed once a session is reviewed


# --------------------------------------------------------------------------- session


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ses"))
    engagement: str
    client: str
    location: str
    count_date: str
    lead: str
    observers: list[str] = Field(default_factory=list)
    status: SessionStatus = SessionStatus.OPEN
    count_instructions: str = ""
    # Difference tolerance for test counts: absolute units and/or percent of client count.
    tolerance_units: float = 0
    tolerance_pct: float = 0
    sample_size: int = 10
    sample_value_threshold: float | None = None
    gps_enabled: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    closed_by: str | None = None
    close_explanations: dict[str, str] = Field(default_factory=dict)
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None


class Zone(BaseModel):
    id: str = Field(default_factory=lambda: new_id("zon"))
    session_id: str
    code: str
    description: str = ""


class ListingItem(BaseModel):
    """A line from the client's perpetual inventory listing."""

    sku: str
    description: str = ""
    location: str = ""  # bin or zone code
    uom: str = "EA"
    quantity: float = 0
    unit_cost: float = 0
    sampled: bool = False
    sample_reason: str | None = None  # "random" | "value_threshold"

    @property
    def extended_value(self) -> float:
        return self.quantity * self.unit_cost


# --------------------------------------------------------------------------- records


class TestCountData(BaseModel):
    __test__ = False  # not a pytest test class

    zone: str | None = None
    direction: Literal["floor_to_sheet", "sheet_to_floor"]
    sku: str
    description: str = ""
    tag_no: str | None = None
    uom: str = "EA"
    client_count: float | None = None
    auditor_count: float
    explanation: str | None = None
    # Computed by the service, never typed.
    difference: float | None = None
    over_tolerance: bool = False


class ProcedureObservationData(BaseModel):
    area: Literal["instructions", "segregation", "movement_freeze", "tag_control"]
    compliant: Literal["yes", "no", "n/a"]
    note: str = ""


class ConditionNoteData(BaseModel):
    zone: str | None = None
    sku: str | None = None
    condition: Literal["damaged", "obsolete", "slow_moving", "consigned"]
    estimated_quantity: float | None = None
    note: str = ""


class CutoffDocumentData(BaseModel):
    type: Literal["receiving", "shipping"]
    document_no: str
    date: str
    last_before_count: bool = True


class TagControlData(BaseModel):
    range_start: int
    range_end: int
    used: list[str] = Field(default_factory=list)  # e.g. ["1001-1180"]
    unused: list[str] | None = None  # None = not recorded yet (an open item)
    voided: list[str] = Field(default_factory=list)


RECORD_DATA_MODELS: dict[RecordType, type[BaseModel]] = {
    RecordType.TEST_COUNT: TestCountData,
    RecordType.PROCEDURE: ProcedureObservationData,
    RecordType.CONDITION: ConditionNoteData,
    RecordType.CUTOFF: CutoffDocumentData,
    RecordType.TAG_CONTROL: TagControlData,
}


class Record(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rec"))
    version: int = 1
    type: RecordType
    session_id: str
    data: dict[str, Any]
    field_sources: dict[str, FieldSource] = Field(default_factory=dict)
    suggestion_id: str | None = None
    author: str
    created_at: datetime = Field(default_factory=utcnow)
    reason: str | None = None  # required from version 2 on
    review_status: Literal["pending", "approved", "queried", "rejected"] = "pending"


# --------------------------------------------------------------------------- evidence


class Evidence(BaseModel):
    """Write-once metadata for a captured photo or clip."""

    id: str = Field(default_factory=lambda: new_id("evd"))
    session_id: str
    kind: Literal["photo", "clip", "derived"]
    media_type: str
    size_bytes: int
    sha256: str
    capture_time: datetime  # device clock
    receipt_time: datetime  # server clock
    clock_gap_seconds: float
    user: str
    device_id: str
    storage_uri: str
    zone: str | None = None
    bin_code: str | None = None
    gps: tuple[float, float] | None = None
    duration_seconds: float | None = None
    burst_frames: int | None = None
    derived_from: str | None = None  # crops/rotations/annotations link to their original
    record_ids: list[str] = Field(default_factory=list)


class Suggestion(BaseModel):
    """AI draft extracted from evidence. Never saved into a record without a person."""

    id: str = Field(default_factory=lambda: new_id("sug"))
    evidence_id: str
    model: str
    created_at: datetime = Field(default_factory=utcnow)
    extract: dict[str, Any]


class ReviewEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rev"))
    record_id: str
    session_id: str
    reviewer: str
    action: ReviewAction
    comment: str = ""
    time: datetime = Field(default_factory=utcnow)


class Device(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dev"))
    session_id: str
    user: str
    user_agent: str = ""
    paired_at: datetime = Field(default_factory=utcnow)
    unpaired_at: datetime | None = None
    battery_level: float | None = None
    queued_uploads: int = 0
    last_seen: datetime | None = None


class OpenItem(BaseModel):
    key: str
    kind: Literal[
        "count_without_evidence",
        "unexplained_difference",
        "tag_unused_not_recorded",
        "no_tag_control",
        "queued_uploads",
        "sample_not_counted",
        "record_queried",
    ]
    message: str
    record_id: str | None = None
    blocking: bool = False  # blocking items cannot be explained away, only resolved
