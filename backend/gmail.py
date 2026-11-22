import os
import smtplib
import ssl
from email.message import EmailMessage


def send(run):
    password = os.environ.get("GARDEN_GMAIL_PASSWORD", "")
    if not password:
        return {"status": "not_configured"}
    sender = os.environ.get("GARDEN_GMAIL_SENDER", "morpalex5@gmail.com")
    recipient = os.environ.get("GARDEN_GMAIL_RECIPIENT", "ar2338881@gmail.com")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "Garden audit " + run["id"] + ": " + run["status"]
    message["Message-ID"] = "<garden-" + run["id"] + "@gmail.com>"
    counts = {level: sum(f.get("severity") == level for f in run["findings"])
              for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}
    message.set_content("\n".join([
        "Garden security audit",
        "Run: " + run["id"],
        "Status: " + run["status"],
        "Findings: " + str(len(run["findings"])),
        *[level + ": " + str(count) for level, count in counts.items()],
        "",
        "Details are available in Garden on the server computer.",
    ]))
    stage = "connecting"
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(sender, password)
            stage = "sending"
            refused = smtp.send_message(message)
            if refused:
                return {"status": "failed", "error": "Recipient refused"}
            stage = "accepted"
        return {"status": "accepted", "note": "Accepted by Gmail SMTP; inbox delivery is not confirmed"}
    except smtplib.SMTPAuthenticationError:
        return {"status": "failed", "error": "Gmail authentication failed; check app password"}
    except smtplib.SMTPResponseException as exc:
        return {"status": "failed", "error": "SMTP rejected the operation", "smtp_code": exc.smtp_code}
    except Exception:
        if stage == "accepted":
            return {"status": "accepted", "note": "Gmail accepted message; connection close failed"}
        return {"status": "unknown" if stage == "sending" else "failed",
                "error": "SMTP connection failed; automatic retries disabled"}
