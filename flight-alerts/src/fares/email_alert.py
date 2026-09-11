"""Email delivery via SMTP.

stdlib only (smtplib + email.message), consistent with the zero-dependency
core: this job holds a mail password, and every dependency added here is
supply-chain surface around that credential.

One digest per sweep, not one message per fare. Five separate emails to a
group thread is how a useful alert becomes a muted one.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

from .decide import Candidate


class MissingEmailConfig(RuntimeError):
    pass


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(cls, env=None) -> "SmtpConfig":
        env = env if env is not None else os.environ
        recipients = tuple(
            addr.strip() for addr in env.get("ALERT_RECIPIENTS", "").split(",")
            if addr.strip()
        )
        missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD") if not env.get(k)]
        if not recipients:
            missing.append("ALERT_RECIPIENTS")
        if missing:
            raise MissingEmailConfig(
                "missing " + ", ".join(missing)
                + " (see README: Gmail app password + recipient list)")
        return cls(
            host=env.get("SMTP_HOST", "smtp.gmail.com"),
            port=int(env.get("SMTP_PORT", "587")),
            user=env["SMTP_USER"],
            password=env["SMTP_PASSWORD"],
            # Gmail rejects a From that isn't the authenticated account.
            sender=env.get("ALERT_FROM") or env["SMTP_USER"],
            recipients=recipients,
        )


def _fare_line(obs, decision) -> str:
    price = decision.effective_price_usd or obs.price_usd
    nights = (obs.ret - obs.depart).days if obs.ret else None
    routing = "nonstop" if obs.is_nonstop else f"{obs.stops} stop"
    parts = [
        f"${price} — {obs.carrier}, {routing}",
        f"  Out {obs.depart:%a %b %-d}"
        + (f" · back {obs.ret:%a %b %-d} ({nights} nights)" if nights else " · one way"),
    ]
    if decision.percentile_rank is not None:
        parts.append(f"  Cheapest {decision.percentile_rank:.0%} of readings "
                     f"we've logged for these dates")
    if obs.typical_low and obs.typical_high:
        parts.append(f"  Google's typical range: ${obs.typical_low}–${obs.typical_high}")
    if price != obs.price_usd:
        parts.append(f"  (${obs.price_usd} fare + ${price - obs.price_usd} bag)")
    return "\n".join(parts)


def format_digest(selected: list[Candidate], origin: str, destination: str,
                  trip_label: str) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html) for one sweep's worth of alerts."""
    if not selected:
        raise ValueError("format_digest called with no alerts")

    cheapest = min(selected, key=lambda c: c[1].effective_price_usd or c[0].price_usd)
    low = cheapest[1].effective_price_usd or cheapest[0].price_usd
    count = len(selected)

    subject = (f"${low} {origin}→{destination} for {trip_label}"
               + (f" (+{count - 1} more)" if count > 1 else ""))

    text = "\n\n".join([
        f"{origin} → {destination} · {trip_label}",
        f"{count} fare{'s' if count > 1 else ''} worth a look:",
        *[_fare_line(o, d) for o, d in selected],
        "Prices move. Check before you get excited:\n"
        f"https://www.google.com/travel/flights?q=flights%20from%20{origin}%20to%20{destination}",
        "— Sent by John's fare tracker. Reply-all to argue about dates.",
    ])

    rows = []
    for obs, decision in selected:
        price = decision.effective_price_usd or obs.price_usd
        nights = (obs.ret - obs.depart).days if obs.ret else None
        detail = [f"{obs.carrier} · {'nonstop' if obs.is_nonstop else f'{obs.stops} stop'}"]
        if nights:
            detail.append(f"{nights} nights")
        if decision.percentile_rank is not None:
            detail.append(f"cheapest {decision.percentile_rank:.0%} on record")
        rows.append(f"""
      <tr>
        <td style="padding:14px 16px;border-bottom:1px solid #e6e6e6;font:600 22px/1.2 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;white-space:nowrap">${price}</td>
        <td style="padding:14px 16px;border-bottom:1px solid #e6e6e6;font:400 14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#111">
          <strong>{obs.depart:%a %b %-d}</strong>{f' &rarr; <strong>{obs.ret:%a %b %-d}</strong>' if obs.ret else ' (one way)'}<br>
          <span style="color:#666">{' · '.join(detail)}</span>
        </td>
      </tr>""")

    html = f"""<!doctype html>
<html><body style="margin:0;padding:24px 12px;background:#f5f5f3">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e0e0dd;border-radius:8px">
    <tr><td style="padding:20px 16px 8px;font:600 15px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;color:#111">
      {origin} &rarr; {destination}
      <div style="font:400 13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#666;padding-top:2px">{trip_label}</div>
    </td></tr>
    <tr><td><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{''.join(rows)}</table></td></tr>
    <tr><td style="padding:16px;font:400 12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#777">
      Prices move fast — verify before booking.
      <a href="https://www.google.com/travel/flights?q=flights%20from%20{origin}%20to%20{destination}" style="color:#0a58ca">Open Google Flights</a><br>
      Sent by John's fare tracker. Reply-all to argue about dates.
    </td></tr>
  </table>
</body></html>"""
    return subject, text, html


def build_message(config: SmtpConfig, subject: str, text: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.sender
    msg["To"] = ", ".join(config.recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _default_send(config: SmtpConfig, msg: EmailMessage) -> None:
    with smtplib.SMTP(config.host, config.port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.user, config.password)
        smtp.send_message(msg)


def send_digest(config: SmtpConfig, selected: list[Candidate], origin: str,
                destination: str, trip_label: str,
                send: Callable[[SmtpConfig, EmailMessage], None] = _default_send) -> str:
    subject, text, html = format_digest(selected, origin, destination, trip_label)
    send(config, build_message(config, subject, text, html))
    return subject
