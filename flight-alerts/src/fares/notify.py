"""ntfy.sh publishing.

Chosen over email for having no credentials to manage: a topic name is the
whole configuration. Note the corollary -- topic names are the only access
control, so an unguessable one is the security model.
"""
from __future__ import annotations

import urllib.request
from typing import Callable

from .models import Decision, Observation

NTFY_BASE = "https://ntfy.sh"


def format_alert(obs: Observation, decision: Decision, origin: str,
                 destination: str) -> tuple[str, str]:
    """Returns (title, body). Leads with the number, since that is what a
    phone notification shows before it is tapped."""
    price = decision.effective_price_usd or obs.price_usd
    nights = (obs.ret - obs.depart).days if obs.ret else None

    title = f"${price} {origin}-{destination} {obs.depart:%b %-d}"
    if nights is not None:
        title += f" +{nights}n"

    lines = [
        f"${price} on {obs.carrier} — {'nonstop' if obs.is_nonstop else f'{obs.stops} stop(s)'}",
        f"Depart {obs.depart:%a %b %-d, %Y}"
        + (f" · return {obs.ret:%a %b %-d}" if obs.ret else " · one way"),
        f"{obs.lead_days} days out",
    ]
    if decision.percentile_rank is not None:
        lines.append(f"Cheaper than {(1 - decision.percentile_rank):.0%} of what we've logged")
    if obs.typical_low and obs.typical_high:
        lines.append(f"Google typical range: ${obs.typical_low}–${obs.typical_high}"
                     + (f" (rated {obs.price_level})" if obs.price_level else ""))
    if price != obs.price_usd:
        lines.append(f"(${obs.price_usd} fare + ${price - obs.price_usd} bag)")
    lines.append(f"[{decision.reason}]")
    return title, "\n".join(lines)


def _default_post(url: str, data: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15):
        pass


def publish(topic: str, title: str, body: str,
            post: Callable[[str, bytes, dict], None] = _default_post) -> None:
    post(f"{NTFY_BASE}/{topic}",
         body.encode("utf-8"),
         {"Title": title, "Tags": "airplane", "Priority": "default"})
