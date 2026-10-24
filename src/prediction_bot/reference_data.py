"""Versioned supplied identity/calendar files; no inferred exchange holidays."""

import hashlib
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

KOLKATA = ZoneInfo("Asia/Kolkata")


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InstrumentAlias(FrozenModel):
    instrument_id: str = Field(min_length=1)
    exchange: Literal["NSE"] = "NSE"
    exchange_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    valid_from: date
    valid_to: date  # Exclusive: use a finite, verified validity horizon.

    @model_validator(mode="after")
    def valid_interval(self) -> "InstrumentAlias":
        if self.valid_to <= self.valid_from:
            raise ValueError("Alias validity must be a nonempty half-open interval")
        return self


class ExchangeDay(FrozenModel):
    session_date: date
    status: Literal["open", "closed"]
    reason: str = Field(min_length=1)
    opens_at: datetime | None = None
    closes_at: datetime | None = None

    @model_validator(mode="after")
    def valid_session(self) -> "ExchangeDay":
        if self.status == "closed":
            if self.opens_at is not None or self.closes_at is not None:
                raise ValueError("Closed dates cannot have trading hours")
        else:
            if self.opens_at is None or self.closes_at is None:
                raise ValueError("Open dates require both session boundaries")
            for boundary in (self.opens_at, self.closes_at):
                if boundary.tzinfo is None or boundary.utcoffset() is None:
                    raise ValueError("Session boundaries must be timezone-aware")
                if boundary.astimezone(KOLKATA).date() != self.session_date:
                    raise ValueError("Session boundaries must lie on the Kolkata session date")
            if self.opens_at >= self.closes_at:
                raise ValueError("Session close must follow open")
        return self


class ReferenceBundle(FrozenModel):
    schema_version: Literal[1] = 1
    source: str = Field(min_length=1)
    published_at: datetime
    coverage_start: date
    coverage_end: date  # Inclusive; every date must be explicitly accounted for.
    aliases: tuple[InstrumentAlias, ...]
    days: tuple[ExchangeDay, ...]

    @model_validator(mode="after")
    def validate_coverage(self) -> "ReferenceBundle":
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("Publication time must be timezone-aware")
        if self.coverage_end < self.coverage_start:
            raise ValueError("Invalid calendar coverage")
        dates = [day.session_date for day in self.days]
        count = (self.coverage_end - self.coverage_start).days + 1
        if len(set(dates)) != len(dates) or len(dates) != count:
            raise ValueError("Calendar must cover every date exactly once")
        if any(day < self.coverage_start or day > self.coverage_end for day in dates):
            raise ValueError("Calendar date is outside declared coverage")
        for index, left in enumerate(self.aliases):
            for right in self.aliases[index + 1 :]:
                overlap = max(left.valid_from, right.valid_from) < min(
                    left.valid_to, right.valid_to
                )
                same_symbol = (left.exchange, left.symbol) == (right.exchange, right.symbol)
                same_id = (left.exchange, left.exchange_id) == (right.exchange, right.exchange_id)
                same_instrument = left.instrument_id == right.instrument_id
                if overlap and (same_symbol or same_id or same_instrument):
                    raise ValueError("Overlapping identity aliases are ambiguous")
        return self

    def resolve(self, symbol: str, on: date, exchange: str = "NSE") -> InstrumentAlias:
        matches = [
            alias
            for alias in self.aliases
            if alias.symbol == symbol
            and alias.exchange == exchange
            and alias.valid_from <= on < alias.valid_to
        ]
        if len(matches) != 1:
            raise LookupError("No unique point-in-time instrument identity")
        return matches[0]

    def day(self, on: date) -> ExchangeDay:
        for day in self.days:
            if day.session_date == on:
                return day
        raise LookupError("Calendar coverage unavailable")

    def session_at(self, instant: datetime) -> ExchangeDay | None:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Event time must be timezone-aware")
        day = self.day(instant.astimezone(KOLKATA).date())
        if day.opens_at is not None and day.closes_at is not None:
            if day.opens_at <= instant < day.closes_at:
                return day
        return None

    def observation_status(self, on: date, *, observed: bool) -> str:
        day = self.day(on)
        if day.status == "closed":
            return "unexpected_observation" if observed else "market_closed"
        return "observed" if observed else "missing_observation"

    def sessions_after(self, on: date, count: int) -> tuple[date, ...]:
        if count < 1:
            raise ValueError("Session count must be positive")
        self.day(on)
        result: list[date] = []
        candidate = on
        while len(result) < count:
            candidate += timedelta(days=1)
            if self.day(candidate).status == "open":
                result.append(candidate)
        return tuple(result)


class ReferenceStore:
    """Immutable snapshots for reproducible lookups and supplied calendar revisions."""

    def __init__(self, directory: Path):
        self.directory = directory

    def import_file(self, path: Path) -> str:
        bundle = ReferenceBundle.model_validate_json(path.read_bytes())
        payload = bundle.model_dump_json().encode("utf-8")
        version = hashlib.sha256(payload).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{version}.json"
        with NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if destination.read_bytes() != payload:
                    raise ValueError("Reference integrity check failed") from None
        finally:
            temporary_path.unlink(missing_ok=True)
        return version

    def get(self, version: str) -> ReferenceBundle:
        if re.fullmatch(r"[a-f0-9]{64}", version) is None:
            raise ValueError("Invalid reference version")
        payload = (self.directory / f"{version}.json").read_bytes()
        if hashlib.sha256(payload).hexdigest() != version:
            raise ValueError("Reference integrity check failed")
        return ReferenceBundle.model_validate_json(payload)
