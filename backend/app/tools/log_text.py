"""Normalize the Kubernetes log response before redaction or persistence."""

LOG_LINES = 1000
LOG_BYTES = 256 * 1024


def sample_log_text(content):
    """Preserve recent raw lines; also bound a single exceptionally long line."""
    lines = content.splitlines(keepends=True)
    sampled = "".join(lines[-LOG_LINES:])
    encoded = sampled.encode("utf-8")
    limited = len(lines) >= LOG_LINES or len(encoded) >= LOG_BYTES
    if len(encoded) > LOG_BYTES:
        sampled = encoded[-LOG_BYTES:].decode("utf-8", errors="ignore")
    return sampled, limited


def decode_log_content(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="strict")
    if isinstance(value, str):
        return value
    raise TypeError("UNSUPPORTED_LOG_RESPONSE_TYPE")


def read_log_response(value):
    """Read raw HTTP responses before SDK string deserialization can repr(bytes)."""
    if isinstance(value, (str, bytes, bytearray)):
        return decode_log_content(value)
    try:
        body = value.read()
        return decode_log_content(body)
    finally:
        try:
            value.close()
        finally:
            value.release_conn()
