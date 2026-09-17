from __future__ import annotations

import logging

from backend.security import log_redaction


def test_redact_url_covers_short_and_provider_signature_parameters() -> None:
    url = (
        "https://public.example/article?sig=sig-secret&key=key-secret&auth=auth-secret"
        "&code=code-secret&sessionid=session-secret&X-Amz-Signature=aws-secret"
        "&topic=public"
    )

    safe = log_redaction.redact_url(url)

    assert "public.example/article" in safe
    assert "topic=public" in safe
    for secret in (
        "sig-secret",
        "key-secret",
        "auth-secret",
        "code-secret",
        "session-secret",
        "aws-secret",
    ):
        assert secret not in safe
    for parameter in ("sig", "key", "auth", "code", "sessionid", "X-Amz-Signature"):
        assert f"{parameter}=%5BREDACTED%5D" in safe


def test_redact_origin_keeps_only_scheme_host_and_explicit_port() -> None:
    url = "https://alice:password@[2001:db8::1]:8443/path?token=secret#fragment"

    assert log_redaction.redact_origin(url) == "https://[2001:db8::1]:8443"


def test_redact_untrusted_text_cleans_quoted_json_secret_keys() -> None:
    body = (
        '{"token":"token-secret","password": "password-secret",'
        '"api_key":"api-secret","safe":"public"}'
    )

    safe = log_redaction.redact_untrusted_text(body)

    assert '"safe":"public"' in safe
    for secret in ("token-secret", "password-secret", "api-secret"):
        assert secret not in safe
    assert safe.count("[REDACTED]") == 3


def test_redacted_marker_prefix_does_not_preserve_secret_suffix() -> None:
    log_redaction.install_log_redaction()
    for key in ("token", "authorization"):
        unsafe = f"{key}=[REDACTED]still-secret"
        expected = f"{key}=[REDACTED]"
        assert log_redaction.redact_untrusted_text(unsafe) == expected
        assert log_redaction.redact_untrusted_text(expected) == expected
        record = logging.makeLogRecord(
            {"name": "httpx.marker.child", "msg": unsafe, "request_detail": unsafe}
        )
        assert record.getMessage() == expected
        assert record.request_detail == expected
        assert "still-secret" not in repr(record.__dict__)


def test_http_record_is_sanitized_after_extra_injection_by_child_logger() -> None:
    log_redaction.install_log_redaction()
    logger = logging.getLogger("httpcore.final-boundary.child")
    handler = _CaptureHandler()
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise RuntimeError(
                "traceback secret password=exception-password "
                "https://alice:password@example.com/path?token=url-secret"
            )
        except RuntimeError:
            logger.error(
                'request body={"token":"body-secret"} %s',
                "https://alice:password@example.com/path?token=url-secret",
                extra={
                    "url": "https://alice:password@example.com/path?token=url-secret",
                    "body": '{"password":"body-secret"}',
                    "nested": {"api_key": "nested-secret"},
                    "items": ["list-secret"],
                    "tuple": ("tuple-secret",),
                },
                exc_info=True,
                stack_info=True,
            )
    finally:
        logger.removeHandler(handler)

    record = handler.records[0]
    rendered = repr(record.__dict__)
    assert record.name == "httpcore.final-boundary.child"
    assert record.levelno == logging.ERROR
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert record.body == "[REDACTED]"
    assert record.nested == "[REDACTED]"
    assert record.items == "[REDACTED]"
    assert record.tuple == "[REDACTED]"
    assert "HTTP client log message redacted" not in record.getMessage()
    for secret in (
        "url-secret",
        "body-secret",
        "nested-secret",
        "list-secret",
        "tuple-secret",
        "exception-password",
    ):
        assert secret not in rendered


def test_make_log_record_boundary_cleans_records_after_dict_injection() -> None:
    log_redaction.install_log_redaction()

    record = logging.makeLogRecord(
        {
            "name": "httpx.transport",
            "levelname": "ERROR",
            "levelno": logging.ERROR,
            "msg": "request",
            "args": (),
            "url": "https://alice:password@example.com/path?token=url-secret",
            "body": {"password": "body-secret"},
        }
    )

    assert record.name == "httpx.transport"
    assert record.url == "https://example.com"
    assert record.body == "[REDACTED]"
    assert "url-secret" not in repr(record.__dict__)
    assert "body-secret" not in repr(record.__dict__)


def test_http_log_urls_are_origin_only_even_with_opaque_paths_and_queries() -> None:
    log_redaction.install_log_redaction()
    logger = logging.getLogger("httpx.origin-only.child")
    handler = _CaptureHandler()
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error(
            "GET %s",
            "https://public.example/token/seed-secret?topic=ai",
            extra={
                "request_url": "https://public.example/api-key/opaque-secret?topic=ai",
                "opaque": "https://public.example/any/private-path?unknown=opaque-secret",
            },
        )
    finally:
        logger.removeHandler(handler)

    record = handler.records[0]
    assert record.getMessage() == "GET https://public.example"
    assert record.request_url == "https://public.example"
    assert record.opaque == "https://public.example"
    rendered = repr(record.__dict__)
    for secret in ("/token/seed-secret", "/api-key/opaque-secret", "/any/private-path"):
        assert secret not in rendered
    assert "?" not in rendered


def test_unrelated_logger_and_existing_custom_factory_remain_unchanged() -> None:
    original_factory = logging.getLogRecordFactory()
    calls: list[bool] = []

    def custom_factory(*args, **kwargs):
        calls.append(True)
        record = original_factory(*args, **kwargs)
        record.custom_marker = "preserved"
        return record

    handler = _CaptureHandler()
    unrelated = logging.getLogger("httpxyz.final-boundary")
    unrelated.addHandler(handler)
    unrelated.propagate = False
    try:
        logging.setLogRecordFactory(custom_factory)
        log_redaction.install_log_redaction()
        unrelated.error(
            "unrelated %s",
            "https://alice:password@example.com/path?token=url-secret",
            extra={"nested": {"secret": "nested-secret"}},
        )
    finally:
        unrelated.removeHandler(handler)
        logging.setLogRecordFactory(original_factory)
        log_redaction.install_log_redaction()

    record = handler.records[0]
    assert calls == [True]
    assert record.custom_marker == "preserved"
    assert record.getMessage().endswith("token=url-secret")
    assert record.nested == {"secret": "nested-secret"}


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
