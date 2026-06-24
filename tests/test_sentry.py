"""
Tests for app/ops/sentry.py — Sentry error monitoring utilities.

No real Sentry network calls are made; all SDK interactions are mocked.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.ops.sentry import capture_exception, init_sentry


# ── init_sentry ────────────────────────────────────────────────────────────────

class TestInitSentry:
    def test_no_dsn_returns_false(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            assert init_sentry("pipeline") is False
            mock_sdk.init.assert_not_called()

    def test_empty_dsn_returns_false(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            assert init_sentry("pipeline") is False
            mock_sdk.init.assert_not_called()

    def test_whitespace_dsn_returns_false(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "   ")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            assert init_sentry("pipeline") is False
            mock_sdk.init.assert_not_called()

    def test_configured_dsn_returns_true(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            assert init_sentry("pipeline") is True
            mock_sdk.init.assert_called_once()

    def test_send_default_pii_is_false(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("pipeline")
            _, kwargs = mock_sdk.init.call_args
            assert kwargs["send_default_pii"] is False

    def test_environment_from_env(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("pipeline")
            _, kwargs = mock_sdk.init.call_args
            assert kwargs["environment"] == "staging"

    def test_environment_defaults_to_production(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("pipeline")
            _, kwargs = mock_sdk.init.call_args
            assert kwargs["environment"] == "production"

    def test_release_from_env(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        monkeypatch.setenv("SENTRY_RELEASE", "v1.2.3")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("pipeline")
            _, kwargs = mock_sdk.init.call_args
            assert kwargs["release"] == "v1.2.3"

    def test_release_defaults_to_none(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        monkeypatch.delenv("SENTRY_RELEASE", raising=False)
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("pipeline")
            _, kwargs = mock_sdk.init.call_args
            assert kwargs["release"] is None

    def test_service_tag_set(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("pipeline")
            mock_sdk.set_tag.assert_called_once_with("porter_service", "pipeline")

    def test_service_tag_reflects_service_name(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            init_sentry("dashboard")
            mock_sdk.set_tag.assert_called_once_with("porter_service", "dashboard")

    def test_init_safe_called_twice(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            result1 = init_sentry("pipeline")
            result2 = init_sentry("pipeline")
        assert result1 is True
        assert result2 is True
        assert mock_sdk.init.call_count == 2  # no guard in our code; SDK handles idempotency


# ── capture_exception ──────────────────────────────────────────────────────────

class TestCaptureException:
    def test_no_dsn_is_noop(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            capture_exception(ValueError("boom"))
            mock_sdk.capture_exception.assert_not_called()

    def test_empty_dsn_is_noop(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            capture_exception(ValueError("boom"))
            mock_sdk.capture_exception.assert_not_called()

    def test_with_dsn_calls_sdk_capture(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        exc = ValueError("test error")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            capture_exception(exc)
            mock_sdk.capture_exception.assert_called_once_with(exc)

    def test_context_extras_attached(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        exc = RuntimeError("source failed")
        ctx = {"source": "usaspending", "source_run_id": "abc-123"}
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            scope_mock = MagicMock()
            mock_sdk.push_scope.return_value.__enter__ = MagicMock(return_value=scope_mock)
            mock_sdk.push_scope.return_value.__exit__ = MagicMock(return_value=False)
            capture_exception(exc, context=ctx)
        scope_mock.set_extra.assert_any_call("source", "usaspending")
        scope_mock.set_extra.assert_any_call("source_run_id", "abc-123")

    def test_no_context_does_not_call_set_extra(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            scope_mock = MagicMock()
            mock_sdk.push_scope.return_value.__enter__ = MagicMock(return_value=scope_mock)
            mock_sdk.push_scope.return_value.__exit__ = MagicMock(return_value=False)
            capture_exception(ValueError("boom"))
        scope_mock.set_extra.assert_not_called()

    def test_sentry_sdk_failure_does_not_propagate(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            mock_sdk.push_scope.side_effect = RuntimeError("sentry internal error")
            # Must not raise — defensive wrapper absorbs SDK failures
            capture_exception(ValueError("original error"))

    def test_capture_exception_failure_does_not_propagate(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        with patch("app.ops.sentry.sentry_sdk") as mock_sdk:
            mock_sdk.push_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_sdk.push_scope.return_value.__exit__ = MagicMock(return_value=False)
            mock_sdk.capture_exception.side_effect = RuntimeError("sentry capture error")
            # Must not raise
            capture_exception(ValueError("original error"))

    def test_does_not_modify_original_exception(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
        exc = ValueError("original error")
        with patch("app.ops.sentry.sentry_sdk"):
            capture_exception(exc)
        assert str(exc) == "original error"
        with pytest.raises(ValueError, match="original error"):
            raise exc


# ── Orchestrator integration ───────────────────────────────────────────────────

class TestOrchestratorSentryIntegration:
    def test_source_failure_is_captured(self):
        """Connector exception → capture_exception called + source_run.status=failed."""
        from app.pipeline.orchestrator import _execute_source

        source = MagicMock()
        source.name = "usaspending"

        source_run = MagicMock()
        source_run.id = uuid.UUID("12345678-1234-5678-1234-567812345678")

        db = MagicMock()
        summary = {"sources_failed": 0, "sources_succeeded": 0, "errors": []}
        error = RuntimeError("connector exploded")

        with patch("app.pipeline.orchestrator.USASpendingConnector") as mock_cls, \
             patch("app.pipeline.orchestrator.capture_exception") as mock_capture:
            mock_cls.return_value.run.side_effect = error
            _execute_source(source, source_run, db, summary)

        mock_capture.assert_called_once()
        captured_exc, captured_ctx = mock_capture.call_args[0]
        assert captured_exc is error
        assert captured_ctx["source"] == "usaspending"
        assert captured_ctx["source_run_id"] == "12345678-1234-5678-1234-567812345678"

        # Existing failed-source behavior preserved
        assert summary["sources_failed"] == 1
        assert source_run.status == "failed"

    def test_source_run_id_none_does_not_crash(self):
        """If source_run.id is None, capture_exception is still called safely."""
        from app.pipeline.orchestrator import _execute_source

        source = MagicMock()
        source.name = "usaspending"

        source_run = MagicMock()
        source_run.id = None  # edge case: id not set

        db = MagicMock()
        summary = {"sources_failed": 0, "sources_succeeded": 0, "errors": []}

        with patch("app.pipeline.orchestrator.USASpendingConnector") as mock_cls, \
             patch("app.pipeline.orchestrator.capture_exception") as mock_capture:
            mock_cls.return_value.run.side_effect = RuntimeError("boom")
            _execute_source(source, source_run, db, summary)

        mock_capture.assert_called_once()
        _, captured_ctx = mock_capture.call_args[0]
        assert captured_ctx["source_run_id"] is None  # graceful None, not a crash

        assert summary["sources_failed"] == 1

    def test_source_failure_errors_summary_unchanged(self):
        """capture_exception call does not alter the errors summary list."""
        from app.pipeline.orchestrator import _execute_source

        source = MagicMock()
        source.name = "usaspending"
        source_run = MagicMock()
        source_run.id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        db = MagicMock()
        summary = {"sources_failed": 0, "sources_succeeded": 0, "errors": []}

        with patch("app.pipeline.orchestrator.USASpendingConnector") as mock_cls, \
             patch("app.pipeline.orchestrator.capture_exception"):
            mock_cls.return_value.run.side_effect = ValueError("bad data")
            _execute_source(source, source_run, db, summary)

        assert len(summary["errors"]) == 1
        assert summary["errors"][0]["source"] == "usaspending"
        assert "bad data" in summary["errors"][0]["error"]
