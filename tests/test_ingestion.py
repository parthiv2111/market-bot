from datetime import UTC, date, datetime

import pytest

from prediction_bot.cli import main
from prediction_bot.ingestion import IngestionPlan, job_identity, run_ingestion
from prediction_bot.reference_data import ExchangeDay, InstrumentAlias, ReferenceBundle

HEADER = b"exchange,symbol,session_date,open,high,low,close,volume\n"
ROW = b"NSE,A,2026-01-02,100,110,90,105,1000\n"
NOW = datetime(2026, 1, 4, tzinfo=UTC)


@pytest.fixture
def reference():
    return ReferenceBundle(
        source="synthetic",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 3),
        aliases=tuple(
            InstrumentAlias(
                instrument_id=s,
                exchange_id=s,
                symbol=s,
                valid_from=date(2026, 1, 1),
                valid_to=date(2027, 1, 1),
            )
            for s in ("A", "B")
        ),
        days=(
            ExchangeDay(
                session_date=date(2026, 1, 2),
                status="open",
                reason="synthetic",
                opens_at="2026-01-02T09:15:00+05:30",
                closes_at="2026-01-02T15:30:00+05:30",
            ),
            ExchangeDay(session_date=date(2026, 1, 3), status="closed", reason="synthetic holiday"),
        ),
    )


@pytest.fixture
def plan(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(HEADER + ROW)
    return IngestionPlan(
        revision="fixture-1",
        mode="historical",
        start=date(2026, 1, 2),
        end=date(2026, 1, 3),
        sources={"A": str(source)},
        usage_basis="synthetic",
    )


def test_repeat_run_skips_completed_symbol_and_keeps_versions(plan, reference, tmp_path):
    calls = []
    first = run_ingestion(
        plan, reference, tmp_path, lambda *args: calls.append(args), now=lambda: NOW
    )
    second = run_ingestion(
        plan,
        reference,
        tmp_path,
        lambda *args: calls.append(args),
        read_source=lambda _: pytest.fail("must use checkpoint"),
    )
    assert second == first
    assert len(calls) == 1
    state = first["symbols"]["A"]
    assert state["status"] == "completed"
    assert state["rows"] == 1
    assert len(state["source_sha256"]) == len(state["validation_version"]) == 64
    assert (tmp_path / "jobs" / first["job_id"] / "plan.json").exists()


def test_interrupted_sink_resumes_from_pinned_source(plan, reference, tmp_path):
    calls = []

    def interrupted(batch, run_id):
        calls.append((batch, run_id))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_ingestion(plan, reference, tmp_path, interrupted, now=lambda: NOW)
    result = run_ingestion(
        plan,
        reference,
        tmp_path,
        lambda *args: calls.append(args),
        read_source=lambda _: pytest.fail("source must be pinned"),
    )
    assert calls[0] == calls[1]
    assert result["symbols"]["A"]["attempts"] == 2
    assert result["status"] == "completed"


def test_failed_symbol_does_not_hide_successful_symbol(plan, reference, tmp_path):
    plan = plan.model_copy(update={"sources": {**plan.sources, "B": "missing.csv"}})
    delays = []
    report = run_ingestion(
        plan, reference, tmp_path, lambda *_: None, sleep=delays.append, now=lambda: NOW
    )
    assert report["status"] == "failed"
    assert report["symbols"]["A"]["status"] == "completed"
    assert report["symbols"]["B"]["error"] == "FileNotFoundError"
    assert report["symbols"]["B"]["attempts"] == 3
    assert delays == [1, 2]
    recovered = run_ingestion(
        plan,
        reference,
        tmp_path,
        lambda *_: None,
        read_source=lambda _: HEADER + ROW.replace(b",A,", b",B,"),
        now=lambda: NOW,
    )
    assert recovered["status"] == "completed"
    assert recovered["symbols"]["A"] == report["symbols"]["A"]
    assert recovered["symbols"]["B"]["attempts"] == 4


def test_transient_read_retry_is_bounded(plan, reference, tmp_path):
    attempts = []
    delays = []

    def read(_):
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("private path")
        return HEADER + ROW

    report = run_ingestion(
        plan,
        reference,
        tmp_path,
        lambda *_: None,
        read_source=read,
        sleep=delays.append,
        now=lambda: NOW,
    )
    assert report["status"] == "completed"
    assert delays == [1, 2]
    assert "private path" not in str(report)


def test_holiday_daily_job_never_reads_sources(plan, reference, tmp_path):
    plan = plan.model_copy(update={"mode": "daily", "start": date(2026, 1, 3)})
    report = run_ingestion(
        plan,
        reference,
        tmp_path,
        lambda *_: pytest.fail("no write"),
        read_source=lambda _: pytest.fail("no read"),
    )
    assert report["symbols"]["A"]["status"] == "skipped"


def test_unknown_calendar_fails_before_read(plan, reference, tmp_path):
    plan = plan.model_copy(update={"end": date(2026, 1, 4)})
    with pytest.raises(LookupError, match="coverage"):
        run_ingestion(plan, reference, tmp_path, lambda *_: pytest.fail("no write"))


@pytest.mark.parametrize(
    "payload", [HEADER, HEADER + ROW + ROW, HEADER + ROW.replace(b",100,", b",-1,")]
)
def test_quality_failure_remains_visible_and_never_reaches_database(
    plan, reference, tmp_path, payload
):
    report = run_ingestion(
        plan,
        reference,
        tmp_path,
        lambda *_: pytest.fail("no write"),
        read_source=lambda _: payload,
        now=lambda: NOW,
    )
    assert report["symbols"]["A"]["error"] == "data_quality"
    assert report["status"] == "failed"


def test_corrupted_snapshot_cannot_resume(plan, reference, tmp_path):
    def interrupt(*_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_ingestion(plan, reference, tmp_path, interrupt, now=lambda: NOW)
    next((tmp_path / "quality" / "raw").iterdir()).write_bytes(b"changed")
    report = run_ingestion(plan, reference, tmp_path, lambda *_: pytest.fail("no write"))
    assert report["symbols"]["A"]["error"] == "ValueError"


def test_revision_and_reference_changes_create_new_job(plan, reference):
    identity = job_identity(plan, reference)
    assert job_identity(plan.model_copy(update={"revision": "new"}), reference) != identity
    assert job_identity(plan, reference.model_copy(update={"source": "corrected"})) != identity


def test_cli_rejects_invalid_plan_without_echoing_contents(tmp_path, capsys):
    path = tmp_path / "plan.json"
    path.write_text('{"secret":"private"}')
    assert main(["ingest", str(path), "--reference", str(path)]) == 2
    output = capsys.readouterr()
    assert "ValidationError" in output.err
    assert "private" not in output.err


def test_unresolved_symbol_is_reported_without_stopping_others(plan, reference, tmp_path):
    plan = plan.model_copy(update={"sources": {**plan.sources, "UNKNOWN": "unused.csv"}})
    report = run_ingestion(plan, reference, tmp_path, lambda *_: None, now=lambda: NOW)
    assert report["symbols"]["A"]["status"] == "completed"
    assert report["symbols"]["UNKNOWN"]["error"] == "instrument_identity"
