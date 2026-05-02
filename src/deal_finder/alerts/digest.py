"""Digest email builder + sender.

Strategy:
  Every tick (e.g. every 3 min):
    1. For each subscriber row (active + confirmed-or-trusted):
       Find all listings where:
         - listing.search_id matches the subscriber's search_id
         - deal_score >= subscriber.score_threshold
         - rejected = FALSE
         - notified = FALSE
    2. If any matched, group ALL matches across that recipient's
       subscriptions into ONE email body. Send via alerts.email.
    3. On success, mark every included listing as notified.

This way:
  - A user with 20 watches who gets 5 deals at once gets ONE email
    with 5 cards, not 5 emails.
  - A user who only gets one deal at this tick gets a one-card email.
  - Subsequent ticks won't re-notify the same listing.

Each subscriber row is one (email, search_id) pair, so the same email
can subscribe to many searches. We GROUP BY email when sending the
digest — that's the key trick.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape

from ..db.connection import get_conn
from .email import send_email

logger = logging.getLogger(__name__)


@dataclass
class DigestMatch:
    listing_id: str
    title: str
    asking_price: float | None
    fair_value: float | None
    deal_score: int
    confidence_label: str | None
    confidence_pm: int | None
    appraisal_note: str | None
    listing_url: str
    photo_url: str | None
    seller_location: str | None
    listed_at: datetime | None
    keyword: str               # the saved-search keyword that matched


def collect_pending_for_email(email: str) -> list[DigestMatch]:
    """All not-yet-notified, score-passing listings for this email's
    subscriptions, across every search they're subscribed to."""
    matches: list[DigestMatch] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.id, l.title, l.price, l.fair_value,
                          l.deal_score, l.appraisal_note,
                          l.listing_url, l.photo_url, l.seller_location,
                          l.listed_at,
                          l.appraisal_breakdown,
                          us.keyword,
                          s.score_threshold
                   FROM subscribers s
                   JOIN user_searches us ON us.id = s.search_id
                   JOIN listings l ON l.search_id = s.search_id
                   WHERE s.email = %s
                     AND s.active = TRUE
                     AND l.appraised = TRUE
                     AND l.rejected = FALSE
                     AND l.notified = FALSE
                     AND l.deal_score IS NOT NULL
                     AND l.deal_score >= s.score_threshold
                   ORDER BY l.deal_score DESC, l.scraped_at DESC""",
                (email,),
            )
            for r in cur.fetchall():
                bd = r[10] or {}
                matches.append(DigestMatch(
                    listing_id=r[0],
                    title=r[1] or "",
                    asking_price=float(r[2]) if r[2] is not None else None,
                    fair_value=float(r[3]) if r[3] is not None else None,
                    deal_score=int(r[4]),
                    confidence_label=bd.get("confidence_label"),
                    confidence_pm=bd.get("confidence_pm"),
                    appraisal_note=r[5],
                    listing_url=r[6] or "",
                    photo_url=r[7],
                    seller_location=r[8],
                    listed_at=r[9],
                    keyword=r[11],
                ))
    return matches


def list_subscriber_emails() -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT email FROM subscribers
                   WHERE active = TRUE
                   ORDER BY email""",
            )
            return [r[0] for r in cur.fetchall()]


def mark_notified(listing_ids: list[str]) -> None:
    if not listing_ids:
        return
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE listings SET
                          notified = TRUE,
                          notified_at = NOW()
                       WHERE id = ANY(%s)""",
                    (listing_ids,),
                )


def send_daily_summaries() -> dict:
    """For each subscriber with daily_summary_enabled, send a summary
    of below-threshold-but-still-scored listings from the past 24 hours.

    These are listings that:
      - were appraised within the last 24 hours
      - did NOT trigger an instant alert (score < subscriber threshold,
        so notified=FALSE not because we owe an alert but because they
        weren't deal-y enough)
      - haven't been summarized yet (summarized_at IS NULL)

    Sends at most one summary per subscriber per 24h cycle (driven by
    last_summary_sent_at).
    """
    sent = 0
    failed = 0
    skipped_empty = 0
    total_listings = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT s.email
                   FROM subscribers s
                   WHERE s.active = TRUE
                     AND s.daily_summary_enabled = TRUE
                     AND (s.last_summary_sent_at IS NULL
                          OR s.last_summary_sent_at < NOW() - INTERVAL '23 hours')""",
            )
            emails = [r[0] for r in cur.fetchall()]

    for email in emails:
        rows = _collect_summary_for_email(email)
        if not rows:
            skipped_empty += 1
            continue

        subject, html, text = _render_summary(email, rows)
        result = send_email(to=email, subject=subject, html=html, text=text)
        if not result.ok:
            failed += 1
            logger.warning("summary send failed to=%s: %s", email, result.message)
            continue

        # Mark listings summarized + bump subscriber's last_summary_sent_at
        listing_ids = [m.listing_id for m in rows]
        with get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE listings SET summarized_at = NOW() WHERE id = ANY(%s)",
                        (listing_ids,),
                    )
                    cur.execute(
                        "UPDATE subscribers SET last_summary_sent_at = NOW() WHERE email = %s",
                        (email,),
                    )
        sent += 1
        total_listings += len(listing_ids)
        logger.info(
            "daily summary sent to=%s n=%d backend=%s",
            email, len(listing_ids), result.backend,
        )

    return {
        "sent": sent,
        "skipped_empty": skipped_empty,
        "failed": failed,
        "total_listings": total_listings,
    }


def _collect_summary_for_email(email: str) -> list[DigestMatch]:
    """Listings appraised in the last 24h on the subscriber's watches
    that scored BELOW their alert threshold (so they weren't sent as
    instant alerts) and haven't been summarized yet.

    The whole point: 'here's what we appraised today that didn't make
    the cut. Did any of these still look interesting to you?'"""
    matches: list[DigestMatch] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.id, l.title, l.price, l.fair_value,
                          l.deal_score, l.appraisal_note,
                          l.listing_url, l.photo_url, l.seller_location,
                          l.listed_at,
                          l.appraisal_breakdown,
                          us.keyword
                   FROM subscribers s
                   JOIN user_searches us ON us.id = s.search_id
                   JOIN listings l ON l.search_id = s.search_id
                   WHERE s.email = %s
                     AND s.active = TRUE
                     AND s.daily_summary_enabled = TRUE
                     AND l.appraised = TRUE
                     AND l.rejected = FALSE
                     AND l.deal_score IS NOT NULL
                     AND l.deal_score < s.score_threshold
                     AND l.summarized_at IS NULL
                     AND l.appraised_at >= NOW() - INTERVAL '24 hours'
                   ORDER BY l.deal_score DESC, l.appraised_at DESC
                   LIMIT 30""",
                (email,),
            )
            for r in cur.fetchall():
                bd = r[10] or {}
                matches.append(DigestMatch(
                    listing_id=r[0],
                    title=r[1] or "",
                    asking_price=float(r[2]) if r[2] is not None else None,
                    fair_value=float(r[3]) if r[3] is not None else None,
                    deal_score=int(r[4]),
                    confidence_label=bd.get("confidence_label"),
                    confidence_pm=bd.get("confidence_pm"),
                    appraisal_note=r[5],
                    listing_url=r[6] or "",
                    photo_url=r[7],
                    seller_location=r[8],
                    listed_at=r[9],
                    keyword=r[11],
                ))
    return matches


def _render_summary(
    email: str, matches: list[DigestMatch],
) -> tuple[str, str, str]:
    n = len(matches)
    subject = f"bullseye: today's appraisals ({n} below your threshold)"
    html = _render_summary_html(matches)
    text = _render_text(matches)
    return subject, html, text


def _render_summary_html(matches: list[DigestMatch]) -> str:
    parts: list[str] = [_HTML_HEAD]
    parts.append(
        f'<h1 style="font-family:Georgia,serif;font-weight:500;letter-spacing:-0.02em;'
        f'margin:0 0 10px;color:#1a1614;">'
        f"Today's appraisals (didn't quite hit your threshold)"
        f'</h1>'
    )
    parts.append(
        f'<p style="color:#6b5d52;margin:0 0 28px;font-size:14px;">'
        f"We scored {len(matches)} listings on your watches in the last 24h. "
        f"None crossed your alert threshold, but some might still be interesting. "
        f"Top scores first."
        f'</p>'
    )

    by_keyword: dict[str, list[DigestMatch]] = {}
    for m in matches:
        by_keyword.setdefault(m.keyword, []).append(m)

    for kw in sorted(by_keyword.keys()):
        parts.append(
            f'<h2 style="font-family:Georgia,serif;font-style:italic;'
            f'font-weight:500;font-size:16px;color:#1a1614;'
            f'margin:24px 0 10px;letter-spacing:0.02em;'
            f'text-transform:lowercase;">'
            f'watch: {escape(kw)}</h2>'
        )
        for m in by_keyword[kw]:
            parts.append(_render_match_card(m))

    parts.append("</div></body></html>")
    return "\n".join(parts)


def send_pending_digests() -> dict:
    """Run one digest cycle. Returns counts for logging."""
    sent = 0
    skipped_empty = 0
    failed = 0
    total_listings = 0

    for email in list_subscriber_emails():
        matches = collect_pending_for_email(email)
        if not matches:
            skipped_empty += 1
            continue
        subject, html, text = render_digest(email, matches)
        result = send_email(to=email, subject=subject, html=html, text=text)
        if not result.ok:
            failed += 1
            logger.warning("digest send failed to=%s: %s", email, result.message)
            continue
        ids = [m.listing_id for m in matches]
        mark_notified(ids)
        sent += 1
        total_listings += len(ids)
        logger.info(
            "digest sent to=%s n=%d backend=%s",
            email, len(ids), result.backend,
        )

    return {
        "sent": sent,
        "skipped_empty": skipped_empty,
        "failed": failed,
        "total_listings": total_listings,
    }


# --- Digest rendering -----------------------------------------------------

def render_digest(
    email: str, matches: list[DigestMatch],
) -> tuple[str, str, str]:
    """Build (subject, html, text) for one recipient's digest."""
    n = len(matches)
    top = matches[0]
    if n == 1:
        subject = f"bullseye: {top.deal_score}/100 — {top.title[:60]}"
    else:
        subject = (
            f"bullseye: {n} new deals "
            f"(top: {top.deal_score}/100 {top.title[:40]})"
        )

    html = _render_html(matches)
    text = _render_text(matches)
    return subject, html, text


def _render_html(matches: list[DigestMatch]) -> str:
    parts: list[str] = [_HTML_HEAD]
    parts.append(
        f'<h1 style="font-family:Georgia,serif;font-weight:500;letter-spacing:-0.02em;'
        f'margin:0 0 18px;color:#1a1614;">'
        f'{len(matches)} new deal{"s" if len(matches) != 1 else ""}'
        f' — bullseye</h1>'
    )
    parts.append(
        '<p style="color:#6b5d52;margin:0 0 28px;font-size:14px;">'
        "Sorted by deal score. Click any listing to open it on Marketplace."
        '</p>'
    )

    by_keyword: dict[str, list[DigestMatch]] = {}
    for m in matches:
        by_keyword.setdefault(m.keyword, []).append(m)

    for kw in sorted(by_keyword.keys()):
        parts.append(
            f'<h2 style="font-family:Georgia,serif;font-style:italic;'
            f'font-weight:500;font-size:16px;color:#1a1614;'
            f'margin:24px 0 10px;letter-spacing:0.02em;'
            f'text-transform:lowercase;">'
            f'watch: {escape(kw)}</h2>'
        )
        for m in by_keyword[kw]:
            parts.append(_render_match_card(m))

    parts.append(
        '<p style="color:#9a8a7d;font-size:11px;margin-top:32px;'
        'border-top:1px solid #ebe2d4;padding-top:14px;">'
        "Don't want these? Reply with 'unsubscribe' or update your preferences "
        "on your bullseye dashboard."
        '</p>'
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def _render_match_card(m: DigestMatch) -> str:
    score = m.deal_score
    color = "#5d7a4f" if score >= 70 else "#c98a3c" if score >= 50 else "#9a8a7d"
    asking = f"${m.asking_price:.0f}" if m.asking_price is not None else "—"
    fair = f"${m.fair_value:.0f}" if m.fair_value is not None else None
    posted = ""
    if m.listed_at and hasattr(m.listed_at, "strftime"):
        # %-d / %-I are POSIX-only; Windows doesn't accept them. Use %d/%I
        # and lstrip the zero ourselves for cross-platform safety.
        d = m.listed_at.strftime("%d").lstrip("0") or "0"
        h = m.listed_at.strftime("%I").lstrip("0") or "0"
        posted = m.listed_at.strftime(f"%b {d}, {h}:%M %p")

    photo_html = ""
    if m.photo_url:
        photo_html = (
            f'<img src="{escape(m.photo_url)}" alt="" '
            f'style="width:100%;max-width:480px;display:block;'
            f'border-radius:8px;margin-bottom:10px;">'
        )

    detail_bits: list[str] = []
    if m.seller_location:
        detail_bits.append(escape(m.seller_location))
    if posted:
        detail_bits.append(f"posted {escape(posted)}")
    if fair:
        detail_bits.append(f"fair {fair}")
    if m.confidence_label:
        c_pm = f" ±{m.confidence_pm}" if m.confidence_pm else ""
        detail_bits.append(f"{escape(m.confidence_label)} confidence{c_pm}")
    detail_line = " · ".join(detail_bits)

    note_html = (
        f'<p style="font-style:italic;color:#1a1614;font-family:Georgia,serif;'
        f'margin:6px 0 0;font-size:13px;line-height:1.4;">'
        f'{escape(m.appraisal_note)}'
        f'</p>'
        if m.appraisal_note else ""
    )

    return (
        '<a href="' + escape(m.listing_url) + '" '
        'style="display:block;text-decoration:none;color:inherit;'
        'border:1px solid #ebe2d4;border-radius:10px;padding:14px;'
        'background:#fff;margin-bottom:12px;">'
        + photo_html +
        f'<div style="display:flex;align-items:baseline;gap:10px;'
        f'flex-wrap:wrap;">'
        f'<span style="font-size:24px;font-weight:700;color:{color};'
        f'font-family:Georgia,serif;letter-spacing:-0.02em;">{score}</span>'
        f'<span style="font-size:10px;text-transform:uppercase;'
        f'letter-spacing:0.10em;color:#6b5d52;font-weight:600;">deal score</span>'
        f'<span style="font-size:14px;color:#1a1614;font-weight:600;'
        f'margin-left:auto;font-family:Georgia,serif;">{asking}</span>'
        f'</div>'
        f'<h3 style="margin:8px 0 4px;font-size:15px;font-weight:500;'
        f'color:#1a1614;">{escape(m.title)}</h3>'
        + (f'<p style="margin:0;font-size:11px;color:#6b5d52;">{detail_line}</p>'
           if detail_line else "")
        + note_html
        + '</a>'
    )


def _render_text(matches: list[DigestMatch]) -> str:
    lines = [f"{len(matches)} new deal(s) — bullseye", ""]
    by_keyword: dict[str, list[DigestMatch]] = {}
    for m in matches:
        by_keyword.setdefault(m.keyword, []).append(m)
    for kw in sorted(by_keyword.keys()):
        lines.append(f"=== {kw} ===")
        for m in by_keyword[kw]:
            asking = f"${m.asking_price:.0f}" if m.asking_price is not None else "—"
            fair = f", fair ${m.fair_value:.0f}" if m.fair_value else ""
            lines.append(f"[{m.deal_score}] {m.title}")
            lines.append(f"    {asking}{fair}  {m.seller_location or ''}")
            if m.appraisal_note:
                lines.append(f"    {m.appraisal_note}")
            lines.append(f"    {m.listing_url}")
            lines.append("")
    return "\n".join(lines)


_HTML_HEAD = (
    '<!doctype html><html><body style="margin:0;background:#f3ead3;'
    'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
    'color:#3f1718;line-height:1.55;">'
    '<div style="max-width:560px;margin:0 auto;padding:32px 24px;">'
)


def send_confirmation_email(
    *,
    email: str,
    name: str | None,
    keywords: list[str],
    radius_km: int,
    home_label: str | None,
    score_threshold: int,
    price_min: int | None = None,
    price_max: int | None = None,
) -> dict:
    """Fire a one-shot 'we're watching for you' email after a save.

    This is the user-facing receipt that the system actually picked up
    their save. Without it the panel just closes silently and the user
    has no idea whether anything happened.

    Returns {ok, backend, message}. Failures don't raise — the save has
    already committed; the email is best-effort.
    """
    if not email or "@" not in email:
        return {"ok": False, "backend": "n/a", "message": "no email"}

    n = len(keywords)
    plural = "watch" if n == 1 else "watches"
    greet = f"Hi {escape(name)}," if name else "Hi,"

    location_line = f"around {escape(home_label)}" if home_label else f"within {radius_km} km of your home"
    price_line = ""
    if price_min and price_max:
        price_line = f" · ${price_min:,}–${price_max:,}"
    elif price_max:
        price_line = f" · under ${price_max:,}"
    elif price_min:
        price_line = f" · ${price_min:,}+"

    kw_html = "".join(
        f'<li style="margin:0 0 4px;font-family:ui-monospace,monospace;font-size:13px;">'
        f'<span style="color:#c2410c;">›</span> {escape(k)}</li>'
        for k in keywords
    )
    kw_text = "\n".join(f"  - {k}" for k in keywords)

    subject = (
        f"bullseye: watching {n} item · alerts at score ≥ {score_threshold}"
        if n == 1 else
        f"bullseye: watching {n} items · alerts at score ≥ {score_threshold}"
    )

    html = (
        _HTML_HEAD
        + f'<div style="display:inline-block;border:1px dashed rgba(63,23,24,0.40);'
          f'padding:4px 10px;border-radius:999px;font-size:9px;letter-spacing:0.18em;'
          f'text-transform:uppercase;color:#6b3a3b;margin-bottom:18px;">'
          f'order ticket · confirmed</div>'
        + f'<h1 style="font-family:Georgia,serif;font-weight:500;font-size:30px;'
          f'letter-spacing:-0.02em;margin:0 0 6px;color:#3f1718;">'
          f'You\'re on watch.</h1>'
        + f'<p style="color:#6b3a3b;font-size:13px;margin:0 0 22px;">'
          f'{greet} we just started monitoring '
          f'{f"<strong>{n}</strong> {plural}" if True else ""}'
          f' for you {escape(location_line)}{escape(price_line)}.'
          f'</p>'
        + f'<div style="text-align:center;color:#9a7070;font-size:10px;'
          f'letter-spacing:0.10em;margin:14px 0;">— · — · — · — · — · — · — · — · — · —</div>'
        + f'<h2 style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;'
          f'color:#c2410c;margin:0 0 10px;font-weight:700;">watching:</h2>'
        + f'<ul style="margin:0 0 22px;padding:0;list-style:none;">{kw_html}</ul>'
        + f'<div style="text-align:center;color:#9a7070;font-size:10px;'
          f'letter-spacing:0.10em;margin:14px 0;">— · — · — · — · — · — · — · — · — · —</div>'
        + f'<p style="color:#3f1718;font-size:13px;margin:0 0 8px;">'
          f'<strong style="font-family:Georgia,serif;font-style:italic;">How alerts work:</strong>'
          f'</p>'
        + f'<ul style="color:#6b3a3b;font-size:12px;margin:0 0 22px;padding-left:18px;line-height:1.7;">'
          f'<li>We poll Marketplace every minute on each watch.</li>'
          f'<li>Every new listing is scored against real comp data.</li>'
          f'<li>You\'ll get an instant email the moment something scores '
          f'<strong style="color:#c2410c;">≥ {score_threshold}</strong>.</li>'
          f'<li>Once a day, we send a summary of everything else we appraised.</li>'
          f'</ul>'
        + f'<p style="color:#9a7070;font-size:10px;letter-spacing:0.10em;'
          f'text-transform:uppercase;border-top:1px dashed rgba(63,23,24,0.18);'
          f'padding-top:14px;margin-top:24px;">'
          f'bullseye · est. 2026 · deterministic appraisal'
          f'</p>'
        + '</div></body></html>'
    )

    text = (
        f"bullseye — you're on watch\n"
        f"{'=' * 40}\n\n"
        f"{greet[:-1] if greet.endswith(',') else greet}\n\n"
        f"We just started monitoring {n} {plural} for you "
        f"{location_line}{price_line}.\n\n"
        f"WATCHING:\n{kw_text}\n\n"
        f"{'-' * 40}\n"
        f"How alerts work:\n"
        f"  * We poll Marketplace every minute on each watch.\n"
        f"  * Every new listing is scored against real comp data.\n"
        f"  * You'll get an instant email when something scores >= {score_threshold}.\n"
        f"  * Once a day, a summary of everything else we appraised.\n\n"
        f"bullseye · est. 2026\n"
    )

    result = send_email(to=email, subject=subject, html=html, text=text)
    out = {"ok": result.ok, "backend": result.backend, "message": result.message}

    # Stamp confirmation_sent_at on the subscriber rows we just created so
    # we don't double-send if the user re-saves the same watch later.
    if result.ok:
        try:
            with get_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE subscribers SET confirmation_sent_at = NOW()
                               WHERE email = %s AND confirmation_sent_at IS NULL""",
                            (email,),
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning("could not stamp confirmation_sent_at: %s", e)

    logger.info(
        "confirmation email to=%s n=%d backend=%s ok=%s",
        email, n, result.backend, result.ok,
    )
    return out
