import hashlib
import ipaddress
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text

from ..config import settings
from ..database import SessionLocal
from ..models import AuthRateLimitEvent


AUTH_RATE_LIMIT_SCOPE = "user_auth"


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


def _trusted_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for value in settings.auth_trusted_proxy_cidrs.split(","):
        candidate = value.strip()
        if candidate:
            networks.append(ipaddress.ip_network(candidate, strict=False))
    return tuple(networks)


def _parse_ip(value: str):
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted(address, networks) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def client_ip(peer_ip: str | None, forwarded_for: str | None) -> str:
    peer = _parse_ip(peer_ip or "")
    if peer is None:
        return "unknown"
    networks = _trusted_networks()
    selected = peer
    if forwarded_for and _is_trusted(peer, networks):
        chain = [_parse_ip(value) for value in forwarded_for.split(",")]
        if all(address is not None for address in chain):
            for address in reversed([*chain, peer]):
                if not _is_trusted(address, networks):
                    selected = address
                    break
    if isinstance(selected, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{selected}/64", strict=False).network_address) + "/64"
    return str(selected)


def _bucket_key(address: str) -> str:
    return hashlib.sha256(f"{AUTH_RATE_LIMIT_SCOPE}:{address}".encode()).hexdigest()


def consume_auth_attempt(address: str, now: datetime | None = None) -> RateLimitDecision:
    current = now or datetime.now(timezone.utc)
    window = timedelta(seconds=settings.auth_rate_limit_window_seconds)
    cutoff = current - window
    bucket = _bucket_key(address)
    lock_value = int.from_bytes(bytes.fromhex(bucket[:16]), "big", signed=True)
    with SessionLocal.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_value)"), {"lock_value": lock_value})
        db.execute(
            delete(AuthRateLimitEvent).where(
                AuthRateLimitEvent.scope == AUTH_RATE_LIMIT_SCOPE,
                AuthRateLimitEvent.bucket_key == bucket,
                AuthRateLimitEvent.occurred_at <= cutoff,
            )
        )
        count = db.scalar(
            select(func.count()).select_from(AuthRateLimitEvent).where(
                AuthRateLimitEvent.scope == AUTH_RATE_LIMIT_SCOPE,
                AuthRateLimitEvent.bucket_key == bucket,
                AuthRateLimitEvent.occurred_at > cutoff,
            )
        ) or 0
        if count >= settings.auth_rate_limit_attempts:
            oldest = db.scalar(
                select(func.min(AuthRateLimitEvent.occurred_at)).where(
                    AuthRateLimitEvent.scope == AUTH_RATE_LIMIT_SCOPE,
                    AuthRateLimitEvent.bucket_key == bucket,
                    AuthRateLimitEvent.occurred_at > cutoff,
                )
            )
            retry_after = max(
                1,
                math.ceil(((oldest + window) - current).total_seconds()) if oldest else 1,
            )
            return RateLimitDecision(False, retry_after)
        db.add(
            AuthRateLimitEvent(
                scope=AUTH_RATE_LIMIT_SCOPE,
                bucket_key=bucket,
                occurred_at=current,
            )
        )
    return RateLimitDecision(True)
