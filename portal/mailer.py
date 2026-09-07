"""Receipt email over SMTP. A no-op unless SMTP_HOST and SMTP_FROM are configured."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable

log = logging.getLogger("portal.mailer")


def send_receipt(settings, to: Iterable[str], subject: str, body: str) -> bool:
    recipients = sorted({a.strip() for a in to if a and "@" in a})
    if not settings.email_enabled or not recipients:
        return False
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            s.ehlo()
            try:
                s.starttls()
                s.ehlo()
            except smtplib.SMTPException:
                pass
            if settings.smtp_user and settings.smtp_password:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return True
    except Exception as exc:  # never fail a submission over email
        log.warning("receipt email failed: %s", exc)
        return False
