import smtplib

from backend import gmail


def test_disabled(monkeypatch):
    monkeypatch.delenv("GARDEN_GMAIL_PASSWORD", raising=False)
    assert gmail.send({})["status"] == "not_configured"


def test_tls_required_and_summary(monkeypatch):
    monkeypatch.setenv("GARDEN_GMAIL_PASSWORD", "test-password")
    events = []

    class SMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.gmail.com", 587)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def ehlo(self):
            events.append("ehlo")
        def starttls(self, context):
            assert context.check_hostname
            events.append("tls")
        def login(self, sender, password):
            assert "tls" in events
            events.append("login")
        def send_message(self, message):
            assert events[-1] == "login"
            assert "secret-marker" not in message.get_content()
            events.append("send")
            return {}

    monkeypatch.setattr(gmail.smtplib, "SMTP", SMTP)
    run = {"id": "a" * 32, "status": "success",
           "findings": [{"severity": "HIGH", "evidence": "secret-marker"}]}
    assert gmail.send(run)["status"] == "accepted"
    assert events == ["ehlo", "tls", "ehlo", "login", "send"]


def test_no_login_if_tls_fails(monkeypatch):
    monkeypatch.setenv("GARDEN_GMAIL_PASSWORD", "test-password")

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def ehlo(self):
            pass
        def starttls(self, context):
            raise smtplib.SMTPNotSupportedError("No TLS")
        def login(self, *args):
            raise AssertionError("Credentials must not be sent")

    monkeypatch.setattr(gmail.smtplib, "SMTP", SMTP)
    result = gmail.send({"id": "a" * 32, "status": "success", "findings": []})
    assert result["status"] == "failed"
