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


def _search_url(origin: str, destination: str, obs) -> str:
    """The exact Google Flights search for this itinerary.

    SerpApi hands back the dated search URL; fall back to a natural-language
    query for older rows recorded before that field was kept. Google Flights
    has no public URL for one selected flight, so the link opens the exact
    date search and the row's flight numbers say which result to pick.
    """
    if obs.url:
        return obs.url
    import urllib.parse
    q = f"flights from {origin} to {destination} on {obs.depart.isoformat()}"
    if obs.ret:
        q += f" returning {obs.ret.isoformat()}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q})


def _clock(stamp: str | None) -> str | None:
    """'2026-12-28 16:34' -> '4:34 PM'."""
    if not stamp or " " not in stamp:
        return None
    hh, mm = stamp.split(" ", 1)[1].split(":")[:2]
    h = int(hh)
    return f"{(h % 12) or 12}:{mm} {'AM' if h < 12 else 'PM'}"


def _next_day(obs) -> bool:
    return bool(obs.depart_time and obs.arrive_time
                and obs.arrive_time[:10] > obs.depart_time[:10])


def _duration(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _leg_routing(stops: int | None, layovers: tuple[str, ...]) -> str:
    if not stops:
        return "nonstop"
    via = f" via {'/'.join(layovers)}" if layovers else ""
    return f"{stops} stop{'s' if stops > 1 else ''}{via}"


def _routing(obs) -> str:
    return _leg_routing(obs.stops, obs.layovers)


def _label(name: str | None, code: str | None, numbers: tuple[str, ...]) -> str:
    """'American AA 3542 / AA 817', or just the carrier if that is all we have."""
    base = name or code or "?"
    return f"{base} {' / '.join(numbers)}" if numbers else base


def _flight_label(obs) -> str:
    return _label(obs.airline, obs.carrier, obs.flight_numbers)


def _ret_label(obs) -> str:
    return _label(obs.ret_airline, None, obs.ret_flight_numbers)


def _leg_times(dep: str | None, arr: str | None, minutes: int | None) -> str | None:
    d, a = _clock(dep), _clock(arr)
    if not (d and a):
        return None
    out = f"{d} → {a}"
    if dep and arr and arr[:10] > dep[:10]:
        out += " (+1 day)"
    if (dur := _duration(minutes)):
        out += f" · {dur}"
    return out


def _times(obs) -> str | None:
    return _leg_times(obs.depart_time, obs.arrive_time, obs.duration_min)


def _ret_times(obs) -> str | None:
    return _leg_times(obs.ret_depart_time, obs.ret_arrive_time, obs.ret_duration_min)


def _fare_line(obs, decision, origin: str = "", destination: str = "") -> str:
    price = decision.effective_price_usd or obs.price_usd
    nights = (obs.ret - obs.depart).days if obs.ret else None
    if obs.has_return:
        parts = [f"${price} — {_flight_label(obs)} out · {_ret_label(obs)} back"]
        out = f"  Out {obs.depart:%a %b %-d}"
        if (t := _times(obs)):
            out += f" {t}"
        parts.append(f"{out} · {_routing(obs)}")
        back = f"  Back {obs.ret:%a %b %-d}"
        if (t := _ret_times(obs)):
            back += f" {t}"
        parts.append(f"{back} · {_leg_routing(obs.ret_stops, obs.ret_layovers)} ({nights} nights)")
    else:
        parts = [f"${price} — {_flight_label(obs)}, {_routing(obs)}"]
        when = f"  Out {obs.depart:%a %b %-d}"
        if (t := _times(obs)):
            when += f" {t}"
        when += f" · back {obs.ret:%a %b %-d} ({nights} nights)" if nights else " · one way"
        parts.append(when)
        if obs.ret:
            parts.append("  (return flight not resolved; price is with the cheapest return)")
    if decision.percentile_rank is not None:
        parts.append(f"  Cheapest {decision.percentile_rank:.0%} of readings "
                     f"we've logged for these dates")
    if obs.typical_low and obs.typical_high:
        parts.append(f"  Google's typical range: ${obs.typical_low}–${obs.typical_high}")
    if price != obs.price_usd:
        parts.append(f"  (${obs.price_usd} fare + ${price - obs.price_usd} bag)")
    parts.append(f"  {_search_url(origin, destination, obs)}")
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
        *[_fare_line(o, d, origin, destination) for o, d in selected],
        "Prices move. Check before you get excited. Each link opens Google Flights "
        "for these exact dates (with the outbound pre-selected where we resolved the "
        "return); match the flight numbers shown.",
        "— Sent by John's fare tracker. Reply-all to argue about dates.",
    ])

    rows = []
    for obs, decision in selected:
        price = decision.effective_price_usd or obs.price_usd
        nights = (obs.ret - obs.depart).days if obs.ret else None
        detail = [_routing(obs)]
        if nights:
            detail.append(f"{nights} nights")
        if decision.percentile_rank is not None:
            detail.append(f"cheapest {decision.percentile_rank:.0%} on record")
        if price != obs.price_usd:
            detail.append(f"${obs.price_usd} fare + ${price - obs.price_usd} bag")
        times = _times(obs)
        link = _search_url(origin, destination, obs)
        if obs.has_return:
            rt = _ret_times(obs)
            legs = (f"<strong>Out {obs.depart:%a %b %-d}</strong> {_flight_label(obs)}"
                    f"{f' · {times}' if times else ''} · {_routing(obs)}<br>"
                    f"<strong>Back {obs.ret:%a %b %-d}</strong> {_ret_label(obs)}"
                    f"{f' · {rt}' if rt else ''} · {_leg_routing(obs.ret_stops, obs.ret_layovers)}<br>")
            detail = [d for d in detail if d != _routing(obs)]
            link_text = "Open on Google Flights (outbound pre-selected)"
        else:
            legs = (f"<strong>{_flight_label(obs)}</strong><br>"
                    f"<strong>{obs.depart:%a %b %-d}</strong>{f' {times}' if times else ''}"
                    f"{f' &rarr; back <strong>{obs.ret:%a %b %-d}</strong>' if obs.ret else ' (one way)'}<br>")
            if obs.ret:
                detail.append("return not resolved; price is with the cheapest return")
            link_text = "Open on Google Flights"
        rows.append(f"""
      <tr>
        <td style="padding:14px 16px;border-bottom:1px solid #e6e6e6;font:600 22px/1.2 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;white-space:nowrap;vertical-align:top">${price}</td>
        <td style="padding:14px 16px;border-bottom:1px solid #e6e6e6;font:400 14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#111">
          {legs}
          <span style="color:#666">{' · '.join(detail)}</span><br>
          <a href="{link}" style="color:#0a58ca">{link_text}</a>
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
      Prices move fast — verify before booking. Links open Google Flights for these exact dates,
      with the outbound pre-selected where the return was resolved; match the flight numbers shown.<br>
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
