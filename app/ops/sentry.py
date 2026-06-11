import os

import sentry_sdk
import structlog

_log = structlog.get_logger(__name__)


def init_sentry(service_name: str) -> bool:
    """Initialize Sentry if SENTRY_DSN is configured. Returns True if initialized."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=False,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE") or None,
    )
    sentry_sdk.set_tag("porter_service", service_name)
    return True


def capture_exception(exc: Exception, context: dict | None = None) -> None:
    """
    Report a caught exception to Sentry with optional safe context.

    No-op when SENTRY_DSN is absent. If the Sentry SDK itself raises,
    the error is logged and swallowed — the original exception path is never affected.
    """
    if not os.environ.get("SENTRY_DSN", "").strip():
        return
    try:
        with sentry_sdk.push_scope() as scope:
            if context:
                for key, value in context.items():
                    scope.set_extra(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:
        _log.warning("sentry_capture_failed")
