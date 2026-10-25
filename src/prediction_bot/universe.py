"""Dated constituent snapshots and explicit evaluation-coverage limitations."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import Field, model_validator

from prediction_bot.data_quality import _aware, _save
from prediction_bot.reference_data import KOLKATA, FrozenModel, ReferenceBundle


class Constituent(FrozenModel):
    instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)


class ConstituentSnapshot(FrozenModel):
    valid_from: date
    valid_to: date  # Exclusive; no extrapolation beyond a supplied interval.
    known_at: datetime
    source: str = Field(min_length=1)
    members: tuple[Constituent, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ConstituentSnapshot":
        _aware(self.known_at)
        if self.valid_to <= self.valid_from:
            raise ValueError("Constituent interval must be nonempty")
        ids = [member.instrument_id for member in self.members]
        symbols = [member.symbol for member in self.members]
        if len(set(ids)) != len(ids) or len(set(symbols)) != len(symbols):
            raise ValueError("Duplicate constituent identity or symbol")
        return self


class HistoryLimitation(FrozenModel):
    instrument_id: str = Field(min_length=1)
    known_at: datetime
    reason: str = Field(min_length=1)  # e.g. missing_delisted_history

    @model_validator(mode="after")
    def validate_time(self) -> "HistoryLimitation":
        _aware(self.known_at)
        return self


class Universe(FrozenModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    name: str = Field(min_length=1)
    snapshots: tuple[ConstituentSnapshot, ...]
    limitations: tuple[HistoryLimitation, ...] = ()

    @model_validator(mode="after")
    def validate_intervals(self) -> "Universe":
        ordered = sorted(self.snapshots, key=lambda item: item.valid_from)
        if any(
            left.valid_to > right.valid_from
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            raise ValueError("Overlapping constituent intervals")
        return self

    def members(self, on: date, *, as_of: datetime) -> tuple[Constituent, ...]:
        _aware(as_of)
        if on > as_of.astimezone(KOLKATA).date():
            raise LookupError("Cannot select a future evaluation date")
        for snapshot in self.snapshots:
            if snapshot.valid_from <= on < snapshot.valid_to and snapshot.known_at <= as_of:
                return snapshot.members
        raise LookupError("Point-in-time constituent coverage unavailable")


class UniverseStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def import_file(self, path: Path) -> str:
        universe = Universe.model_validate_json(path.read_bytes())
        return _save(self.directory, universe.model_dump_json().encode(), "json")

    def get(self, version: str) -> Universe:
        if re.fullmatch(r"[a-f0-9]{64}", version) is None:
            raise ValueError("Invalid universe version")
        payload = (self.directory / f"{version}.json").read_bytes()
        if hashlib.sha256(payload).hexdigest() != version:
            raise ValueError("Universe integrity check failed")
        return Universe.model_validate_json(payload)


class AvailableObservation(FrozenModel):
    instrument_id: str = Field(min_length=1)
    session_date: date
    known_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> "AvailableObservation":
        _aware(self.known_at)
        if self.session_date > self.known_at.astimezone(KOLKATA).date():
            raise ValueError("An observation cannot be known before its session date")
        return self


@dataclass(frozen=True)
class CoverageRow:
    session_date: date
    instrument_id: str | None
    symbol: str | None
    sufficient: bool
    missing_history: tuple[date, ...]
    missing_future: tuple[date, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CoverageReport:
    rows: tuple[CoverageRow, ...]
    limitations: tuple[str, ...]
    universe_version: str
    reference_version: str
    observations_version: str
    as_of: datetime
    start: date
    end: date
    history_sessions: int
    future_sessions: int

    def save(self, path: Path) -> str:
        return _save(path, json.dumps(asdict(self), sort_keys=True, default=str).encode(), "json")


def coverage_report(
    universe: Universe,
    reference: ReferenceBundle,
    observations: tuple[AvailableObservation, ...],
    *,
    start: date,
    end: date,
    as_of: datetime,
    history_sessions: int = 1,
    future_sessions: int = 5,
) -> CoverageReport:
    """Membership at each close; history available at that close, labels by report cutoff."""
    _aware(as_of)
    if end < start or end > as_of.astimezone(KOLKATA).date():
        raise ValueError("Invalid evaluation range")
    if history_sessions < 1 or future_sessions < 1:
        raise ValueError("Require positive history and future session counts")
    available: dict[tuple[str, date], datetime] = {}
    for observation in observations:
        key = (observation.instrument_id, observation.session_date)
        previous = available.get(key)
        available[key] = min(previous, observation.known_at) if previous else observation.known_at
    rows = []
    day = start
    while day <= end:
        try:
            session = reference.day(day)
        except LookupError:
            rows.append(CoverageRow(day, None, None, False, (), (), ("calendar_unavailable",)))
            day += timedelta(days=1)
            continue
        if session.status == "closed":
            day += timedelta(days=1)
            continue
        assert session.closes_at is not None
        if session.closes_at > as_of:
            rows.append(CoverageRow(day, None, None, False, (), (), ("session_not_closed",)))
            day += timedelta(days=1)
            continue
        decision = min(session.closes_at, as_of)
        try:
            members = universe.members(day, as_of=decision)
        except LookupError:
            rows.append(CoverageRow(day, None, None, False, (), (), ("universe_unavailable",)))
            day += timedelta(days=1)
            continue
        history = [day]
        before = day
        calendar_missing = False
        while len(history) < history_sessions:
            before -= timedelta(days=1)
            try:
                if reference.day(before).status == "open":
                    history.append(before)
            except LookupError:
                calendar_missing = True
                break
        try:
            future = reference.sessions_after(day, future_sessions)
        except LookupError:
            future = ()
            calendar_missing = True
        for member in members:
            reasons = []
            try:
                if reference.resolve(member.symbol, day).instrument_id != member.instrument_id:
                    reasons.append("identity_mismatch")
            except LookupError:
                reasons.append("identity_unavailable")
            missing_history = tuple(
                sorted(
                    d
                    for d in history
                    if (member.instrument_id, d) not in available
                    or available[(member.instrument_id, d)] > decision
                )
            )
            missing_future = tuple(
                d
                for d in future
                if (member.instrument_id, d) not in available
                or available[(member.instrument_id, d)] > as_of
            )
            if calendar_missing:
                reasons.append("window_calendar_unavailable")
            if missing_history:
                reasons.append("missing_history")
            if missing_future:
                reasons.append("missing_future")
            reasons.extend(
                item.reason
                for item in universe.limitations
                if item.instrument_id == member.instrument_id and item.known_at <= as_of
            )
            rows.append(
                CoverageRow(
                    day,
                    member.instrument_id,
                    member.symbol,
                    not reasons,
                    missing_history,
                    missing_future,
                    tuple(reasons),
                )
            )
        day += timedelta(days=1)
    limitations = tuple(
        sorted(
            {
                f"{item.instrument_id}: {item.reason}"
                for item in universe.limitations
                if item.known_at <= as_of
            }
        )
    )
    observations_payload = json.dumps(
        [item.model_dump(mode="json") for item in observations], sort_keys=True
    ).encode()
    return CoverageReport(
        tuple(rows),
        limitations,
        hashlib.sha256(universe.model_dump_json().encode()).hexdigest(),
        hashlib.sha256(reference.model_dump_json().encode()).hexdigest(),
        hashlib.sha256(observations_payload).hexdigest(),
        as_of,
        start,
        end,
        history_sessions,
        future_sessions,
    )
