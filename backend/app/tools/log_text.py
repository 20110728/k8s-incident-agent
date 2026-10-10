"""Normalize the Kubernetes log response before redaction or persistence."""


def decode_log_content(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="strict")
    if isinstance(value, str):
        return value
    raise TypeError("UNSUPPORTED_LOG_RESPONSE_TYPE")
