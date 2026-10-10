"""Normalize the Kubernetes log response before redaction or persistence."""


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
