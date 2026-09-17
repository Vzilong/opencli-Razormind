"""Central logging safeguards for untrusted HTTP failure details."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_URL_PARAMETERS = frozenset(
    {
        "apikey",
        "accesstoken",
        "auth",
        "authorization",
        "bearer",
        "code",
        "clientsecret",
        "credential",
        "key",
        "password",
        "passwd",
        "refreshtoken",
        "secret",
        "sessionid",
        "sig",
        "signature",
        "token",
        "xamzsignature",
    }
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/\s@]+@")
_QUOTED_SENSITIVE_ASSIGNMENT = re.compile(
    r'''(?ix)(
        ["']?(?:authorization|api[_-]?key|access[_-]?token|bearer|client[_-]?secret|
        credential|password|passwd|refresh[_-]?token|secret|session[_-]?id|sig(?:nature)?|
        x[-_]?amz[-_]?signature|token|auth|code)["']?\s*[:=]\s*
    )(["'])(.*?)(\2)'''
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)((?:authorization|api[_-]?key|access[_-]?token|bearer|client[_-]?secret|"
    r"credential|password|passwd|refresh[_-]?token|secret|session[_-]?id|sig(?:nature)?|"
    r"x[-_]?amz[-_]?signature|token|auth|code)\s*[:=]\s*)([^\s,;&}\x27\x22]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)([^\s,;&]+)")
_URL_IN_TEXT = re.compile(r"(?i)https?://[^\s\"'<>]+")
_SENSITIVE_VALUE_MARKER = re.compile(
    r"(?i)\b(?:api[-_ ]?key|authorization|bearer|clientsecret|password|passwd|"
    r"refreshtoken|secret|session[-_ ]?id|signature|sig|token|auth|code)\b"
)
_INSTALL_LOCK = threading.Lock()
_FACTORY_MARKER = "_opencli_log_redactor"
_MAKE_RECORD_MARKER = "_opencli_log_redaction_make_record"
_MAKE_LOG_RECORD_MARKER = "_opencli_log_redaction_make_log_record"
_REDACTED = "[REDACTED]"
_SENSITIVE_EXTRA_NAMES = frozenset(
    {
        "body",
        "content",
        "headers",
        "payload",
        "request",
        "requestbody",
        "requestheaders",
        "response",
        "responsebody",
        "responseheaders",
    }
)
_SAFE_FACTORY_ATTRIBUTES = frozenset(
    {*logging.LogRecord("", 0, "", 0, "", (), None).__dict__}
)


def _redact_secret_tokens(value: str) -> str:
    value = _URL_USERINFO.sub(r"\1", value)
    value = _BEARER_VALUE.sub(rf"\1{_REDACTED}", value)
    value = _QUOTED_SENSITIVE_ASSIGNMENT.sub(rf"\1\2{_REDACTED}\4", value)
    return _SENSITIVE_ASSIGNMENT.sub(_replace_sensitive_assignment, value)


def _replace_sensitive_assignment(match: re.Match[str]) -> str:
    current = match.group(2)
    if current.casefold() == "bearer" or current == _REDACTED:
        return match.group(0)
    return f"{match.group(1)}{_REDACTED}"


def _is_sensitive_parameter(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized in _SENSITIVE_URL_PARAMETERS:
        return True
    # Keep the useful ``provider_api_key``/``x_session_id`` convention while
    # avoiding short suffixes such as ``design`` matching ``sig`` or ``monkey``
    # matching ``key``.
    return any(len(name) > 3 and normalized.endswith(name) for name in _SENSITIVE_URL_PARAMETERS)


def redact_url(url: str) -> str:
    """Remove URL credentials and redact secret-like query values."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        if not hostname:
            return _redact_secret_tokens(url)[:2_000]
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        query = [
            (
                key,
                "[REDACTED]"
                if _is_sensitive_parameter(key) or _SENSITIVE_VALUE_MARKER.search(value)
                else value,
            )
            for key, value in parse_qsl(parts.query.replace(";", "&"), keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))[:2_000]
    except ValueError:
        return redact_origin(url)


def redact_origin(url: str) -> str:
    """Return only a URL origin, dropping all userinfo and resource details."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        if not hostname:
            return ""
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        return urlunsplit((parts.scheme, host, "", "", ""))
    except ValueError:
        return ""


def redact_untrusted_text(value: str) -> str:
    """Redact credentials and secret-bearing URLs from diagnostic text."""
    value = _redact_secret_tokens(value)
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), value)[:2_000]


def _redact_http_log_text(value: str) -> str:
    """Keep HTTP log URLs at origin scope, independent of path/query shape."""
    value = _redact_secret_tokens(value)
    return _URL_IN_TEXT.sub(lambda match: redact_origin(match.group(0)), value)[:2_000]


def _is_http_logger(name: object) -> bool:
    return isinstance(name, str) and (
        name == "httpx"
        or name.startswith("httpx.")
        or name == "httpcore"
        or name.startswith("httpcore.")
    )


def _sanitize_record(record: logging.LogRecord) -> logging.LogRecord:
    """Sanitize HTTP records after ``extra`` has been attached."""
    if not _is_http_logger(record.name):
        return record
    exception_type_name: str | None = None
    try:
        message = record.getMessage()
    except Exception:
        message = "HTTP client log message redacted"
    record.msg = _redact_http_log_text(str(message))
    record.args = ()
    if record.exc_info:
        try:
            exception_type = record.exc_info[0]
        except (IndexError, TypeError):
            exception_type = None
        exception_type_name = getattr(exception_type, "__name__", "Exception")
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    for key, value in tuple(record.__dict__.items()):
        if key in _SAFE_FACTORY_ATTRIBUTES:
            continue
        if key.lower().replace("_", "") in _SENSITIVE_EXTRA_NAMES:
            setattr(record, key, _REDACTED)
            continue
        if isinstance(value, str):
            setattr(record, key, _redact_http_log_text(value))
        else:
            setattr(record, key, _REDACTED)
    if exception_type_name is not None:
        record.exception_type = exception_type_name
    return record


def _install_make_record_boundary() -> None:
    """Sanitize records after ``Logger.makeRecord`` copies ``extra`` fields."""
    current = logging.Logger.makeRecord
    if not getattr(current, _MAKE_RECORD_MARKER, False):

        def redacting_make_record(
            self: logging.Logger, *args: Any, **kwargs: Any
        ) -> logging.LogRecord:
            record = current(self, *args, **kwargs)
            return _sanitize_record(record)

        setattr(redacting_make_record, _MAKE_RECORD_MARKER, True)
        logging.Logger.makeRecord = redacting_make_record

    current_make_log_record = logging.makeLogRecord
    if not getattr(current_make_log_record, _MAKE_LOG_RECORD_MARKER, False):

        def redacting_make_log_record(record_dict: dict[str, Any]) -> logging.LogRecord:
            record = current_make_log_record(record_dict)
            return _sanitize_record(record)

        setattr(redacting_make_log_record, _MAKE_LOG_RECORD_MARKER, True)
        logging.makeLogRecord = redacting_make_log_record


def install_log_redaction() -> Callable[..., logging.LogRecord]:
    """Install the HTTP log redactor once, preserving any prior factory."""
    with _INSTALL_LOCK:
        _install_make_record_boundary()
        predecessor = logging.getLogRecordFactory()
        if getattr(predecessor, _FACTORY_MARKER, False):
            return predecessor

        def redacting_factory(
            *args: Any,
            _predecessor: Callable[..., logging.LogRecord] = predecessor,
            **kwargs: Any,
        ) -> logging.LogRecord:
            return _sanitize_record(_predecessor(*args, **kwargs))

        setattr(redacting_factory, _FACTORY_MARKER, True)
        logging.setLogRecordFactory(redacting_factory)
        return redacting_factory


__all__ = ["install_log_redaction", "redact_origin", "redact_untrusted_text", "redact_url"]
