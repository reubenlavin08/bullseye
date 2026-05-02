"""Email delivery layer.

Three backends, picked via ALERT_BACKEND env var:

  console   (default — prints the email body to stdout, no SMTP needed,
             great for development and the first-time setup smoke test)
  smtp      (any SMTP server: Gmail, Resend SMTP, Fastmail, etc.)
  resend    (Resend's HTTP API — needs RESEND_API_KEY)

Pick whichever you have credentials for. The digest builder calls
`send_email(to, subject, html, text)` and doesn't care which backend
ends up dispatching.
"""
from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import requests

logger = logging.getLogger(__name__)


@dataclass
class EmailResult:
    ok: bool
    backend: str
    message: str = ""


def send_email(*, to: str, subject: str, html: str, text: str = "") -> EmailResult:
    """Dispatch one email via the configured backend."""
    backend = os.environ.get("ALERT_BACKEND", "console").lower()
    if backend == "console":
        return _send_console(to=to, subject=subject, html=html, text=text)
    if backend == "smtp":
        return _send_smtp(to=to, subject=subject, html=html, text=text)
    if backend == "resend":
        return _send_resend(to=to, subject=subject, html=html, text=text)
    return EmailResult(ok=False, backend=backend,
                       message=f"unknown ALERT_BACKEND: {backend}")


# --- console (DRY-RUN) ----------------------------------------------------

def _send_console(*, to: str, subject: str, html: str, text: str) -> EmailResult:
    body = text or html
    # Windows cp1252 consoles can't encode unicode like '≥' or '·'.
    # Encode-decode with 'replace' so the dump never crashes.
    def _safe(s: str) -> str:
        try:
            return s.encode("utf-8", "replace").decode(
                getattr(__import__("sys").stdout, "encoding", "utf-8") or "utf-8",
                "replace",
            )
        except Exception:  # noqa: BLE001
            return s.encode("ascii", "replace").decode("ascii")

    print("\n" + "=" * 72)
    print(_safe(f"To:      {to}"))
    print(_safe(f"Subject: {subject}"))
    print("-" * 72)
    print(_safe(body[:4000]))
    if len(body) > 4000:
        print(f"... ({len(body)-4000} more chars truncated)")
    print("=" * 72 + "\n")
    return EmailResult(ok=True, backend="console", message="printed to stdout")


# --- SMTP -----------------------------------------------------------------

def _send_smtp(*, to: str, subject: str, html: str, text: str) -> EmailResult:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("ALERT_FROM_EMAIL") or user
    if not (host and user and password and from_addr):
        return EmailResult(
            ok=False, backend="smtp",
            message="SMTP_HOST / SMTP_USER / SMTP_PASSWORD / ALERT_FROM_EMAIL required",
        )

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or _strip_html(html))
    msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as e:  # noqa: BLE001
        logger.exception("SMTP send failed")
        return EmailResult(ok=False, backend="smtp", message=str(e))
    return EmailResult(ok=True, backend="smtp", message="sent via SMTP")


# --- Resend HTTP API ------------------------------------------------------

def _send_resend(*, to: str, subject: str, html: str, text: str) -> EmailResult:
    api_key = os.environ.get("RESEND_API_KEY")
    from_addr = os.environ.get("ALERT_FROM_EMAIL")
    if not api_key or not from_addr:
        return EmailResult(
            ok=False, backend="resend",
            message="RESEND_API_KEY and ALERT_FROM_EMAIL required",
        )
    payload = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.exception("Resend send failed")
        return EmailResult(ok=False, backend="resend", message=str(e))
    return EmailResult(ok=True, backend="resend",
                       message=f"sent via Resend ({r.json().get('id', '?')})")


# --- helpers --------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Best-effort plain-text fallback for SMTP multipart."""
    import re
    t = re.sub(r"<[^>]+>", "", html)
    t = re.sub(r"\s+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
