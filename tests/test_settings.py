import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from prediction_bot.cli import main
from prediction_bot.policies import PolicyStore, SessionPolicy, SettingsManager
from prediction_bot.settings import ConfigurationError, Settings, load_settings, policy_payload


@pytest.fixture
def configuration():
    path = Path(__file__).resolve().parents[1] / "config/research.example.json"
    return json.loads(path.read_text())


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), 101, "bad", True])
@pytest.mark.parametrize(
    "field",
    [
        "per_trade_risk_pct",
        "portfolio_heat_pct",
        "daily_loss_limit_pct",
        "weekly_loss_limit_pct",
        "monthly_loss_limit_pct",
        "drawdown_stop_pct",
        "profit_protect_pct",
    ],
)
def test_invalid_percentages_rejected(configuration, field, value):
    configuration["risk"][field] = value
    with pytest.raises(ValidationError):
        Settings.model_validate(configuration)


def test_missing_risk_and_incoherent_limits_rejected(configuration):
    with pytest.raises(ValidationError):
        Settings()
    configuration["risk"]["per_trade_risk_pct"] = 2
    with pytest.raises(ValidationError):
        Settings.model_validate(configuration)


def test_each_risk_percentage_is_required(configuration):
    for field in configuration["risk"]:
        if field == "exceptions_enabled":
            continue
        incomplete = json.loads(json.dumps(configuration))
        del incomplete["risk"][field]
        with pytest.raises(ValidationError):
            Settings.model_validate(incomplete)


def test_cli_validation_never_prints_secret(configuration, tmp_path, capsys):
    configuration["provider_api_key"] = "secret-provider-key"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(configuration))
    assert main(["validate-config", str(path)]) == 0
    configuration["mode"] = "secret-provider-key"
    path.write_text(json.dumps(configuration))
    assert main(["validate-config", str(path)]) == 2
    output = capsys.readouterr()
    assert "secret-provider-key" not in output.out + output.err


def test_session_binding_concurrency_and_corruption(configuration, tmp_path):
    manager = SettingsManager(PolicyStore(tmp_path), Settings.model_validate(configuration))
    with ThreadPoolExecutor(max_workers=4) as executor:
        bindings = list(executor.map(manager.bind_new_session, ["same-session"] * 8))
    assert len(set(bindings)) == 1
    next((tmp_path / "sessions").iterdir()).write_text("invalid")
    with pytest.raises(ValueError, match="version"):
        manager.bind_new_session("same-session")


def test_defaults_and_disabled_capabilities(configuration):
    del configuration["mode"]
    settings = Settings.model_validate(configuration)
    assert settings.mode == "paper"
    assert settings.risk.exceptions_enabled is False
    for mode in ["live", "disabled-live", "unknown"]:
        with pytest.raises(ValidationError):
            Settings.model_validate({**configuration, "mode": mode})
    configuration["risk"]["exceptions_enabled"] = True
    with pytest.raises(ValidationError):
        Settings.model_validate(configuration)


def test_secrets_redacted_from_records_and_errors(configuration, tmp_path):
    secret = "do-not-log-this-sensitive-value"
    configuration["provider_api_key"] = secret
    configuration["database_url"] = secret
    settings = Settings.model_validate(configuration)
    assert secret not in repr(settings)
    assert secret not in settings.model_dump_json()
    assert secret.encode() not in policy_payload(settings)
    configuration["risk"]["daily_loss_limit_pct"] = secret
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(configuration))
    with pytest.raises(ConfigurationError):
        try:
            load_settings(path)
        except ConfigurationError:
            assert secret not in traceback.format_exc()
            raise


def test_policy_versions_persist_and_are_immutable(configuration, tmp_path):
    settings = Settings.model_validate(configuration)
    store = PolicyStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        versions = list(executor.map(store.save, [settings] * 8))
    assert len(set(versions)) == 1
    version = versions[0]
    assert PolicyStore(tmp_path).get(version) == settings
    with pytest.raises(ValidationError):
        settings.risk.daily_loss_limit_pct = 2
    (tmp_path / f"{version}.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        store.get(version)
    with pytest.raises(ValueError, match="integrity"):
        store.save(settings)


def test_reload_preserves_existing_session_and_queued_policy(configuration, tmp_path):
    store = PolicyStore(tmp_path / "policies")
    manager = SettingsManager(store, Settings.model_validate(configuration))
    binding = manager.bind_new_session("2026-10-23")
    queued_policy = binding.version
    configuration["risk"]["daily_loss_limit_pct"] = 2
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configuration))
    manager.reload(path)
    assert manager.bind_new_session(binding.session_id) == binding
    assert manager.resolve(binding).risk.daily_loss_limit_pct == 1
    assert store.get(queued_policy).risk.daily_loss_limit_pct == 1
    assert manager.resolve(manager.bind_new_session("2026-10-26")).risk.daily_loss_limit_pct == 2
    restarted = SettingsManager(store, Settings.model_validate(configuration))
    assert restarted.bind_new_session(binding.session_id) == binding
    restored = SessionPolicy(binding.session_id, binding.version)
    assert restarted.resolve(restored).risk.daily_loss_limit_pct == 1
    with pytest.raises(FrozenInstanceError):
        binding.version = manager.pending_version
    path.write_text("invalid")
    previous = manager.pending_version
    with pytest.raises(ConfigurationError):
        manager.reload(path)
    assert manager.pending_version == previous


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError, match="version"):
        PolicyStore(tmp_path).get("../secret")
