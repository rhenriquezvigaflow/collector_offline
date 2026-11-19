import logging

from collector_offline.observability import JsonFormatter, RedactingFormatter


def test_credentials_in_driver_errors_and_extra_fields_are_redacted() -> None:
    secret = 'password-with-"quotes"'
    formatter = RedactingFormatter(JsonFormatter(), [secret])
    record = logging.makeLogRecord(
        {"msg": f"driver failed: {secret}", "levelname": "ERROR", "error": {"detail": secret}}
    )
    output = formatter.format(record)
    assert secret not in output
    assert "[redacted]" in output
    assert "password-with" not in output
