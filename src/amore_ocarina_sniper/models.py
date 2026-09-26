"""Domain models shared across bridge, coordinator, adapter, and store."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


class PurchaseState(enum.StrEnum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    VERIFYING = "VERIFYING"
    CHECKOUT_READY = "CHECKOUT_READY"
    SUBMITTING = "SUBMITTING"
    PURCHASED = "PURCHASED"
    UNKNOWN = "UNKNOWN"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


# States in which a new trigger may start work. Everything else rejects triggers.
TRIGGERABLE_STATES = frozenset({PurchaseState.ARMED})
# States that are "busy" – another attempt is in flight; new triggers are ignored.
BUSY_STATES = frozenset({PurchaseState.VERIFYING, PurchaseState.CHECKOUT_READY, PurchaseState.SUBMITTING})
# Terminal-ish states that require an explicit operator command to leave.
STICKY_STATES = frozenset({PurchaseState.PURCHASED, PurchaseState.UNKNOWN, PurchaseState.NEEDS_ATTENTION})


@dataclass(frozen=True)
class TriggerEvent:
    """A validated trigger delivered by the extension (or the synthetic CLI)."""

    event_id: str
    message_id: str
    channel_id: str
    guild_id: str
    matched_target: str
    message_ts_ms: int  # rendered Discord timestamp (epoch ms, UTC)
    sent_at_ms: int  # extension wall clock at send time (epoch ms)
    detected_at_offset_ms: int  # extension-side monotonic delta: detection -> send
    attempt: int = 1
    source: str = "extension"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OfferSnapshot:
    """What the product page says right now. None = unreadable (fail closed)."""

    asin: str | None
    title: str | None
    price: Decimal | None
    currency: str | None
    seller: str | None
    seller_id: str | None
    fulfiller: str | None
    condition: str | None
    availability: str | None
    in_stock: bool
    has_variant_selector: bool
    buy_now_available: bool
    add_to_cart_available: bool
    raw: dict[str, Any] = field(default_factory=dict)
    # Availability text announces a release date / "Pre-order now" (observed live 2026-09-25
    # 03:52 on the target ASIN). Purchasable only when policy.allow_preorder is set.
    is_preorder: bool = False


@dataclass
class CheckoutSnapshot:
    """What the order review page says immediately before submission."""

    asin: str | None
    title: str | None
    seller: str | None
    fulfiller: str | None
    condition: str | None
    quantity: int | None
    currency: str | None
    item_price: Decimal | None
    total: Decimal | None
    address_text: str | None
    payment_text: str | None
    line_item_count: int | None
    place_order_available: bool
    raw: dict[str, Any] = field(default_factory=dict)
    # A *visible* security-code / card-number input on the review page: Amazon wants a human
    # to re-enter card details. We never store or type those, so this fails closed before
    # the click instead of after it.
    payment_input_required: bool = False


@dataclass
class PolicyDecision:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(ok=True, reasons=[])

    @classmethod
    def deny(cls, *reasons: str) -> PolicyDecision:
        return cls(ok=False, reasons=list(reasons))


class ChallengeKind(enum.StrEnum):
    CAPTCHA = "captcha"
    LOGIN_REQUIRED = "login_required"
    MFA = "mfa"
    ACCESS_DENIED = "access_denied"
    PAYMENT_CHALLENGE = "payment_challenge"
    # Amazon's own error page ("Sorry! Something went wrong", CloudFront 5xx): overload, not a
    # human check. Transient like UNKNOWN_PAGE; never parks the bot.
    SERVER_ERROR = "server_error"
    UNKNOWN_PAGE = "unknown_page"

    @property
    def transient(self) -> bool:
        return self in (ChallengeKind.UNKNOWN_PAGE, ChallengeKind.SERVER_ERROR)


class ChallengeDetected(Exception):
    """Raised by the Amazon adapter whenever a human must intervene. Never bypassed."""

    def __init__(self, kind: ChallengeKind, detail: str = "") -> None:
        super().__init__(f"{kind.value}: {detail}".rstrip(": "))
        self.kind = kind
        self.detail = detail


class SubmissionAmbiguous(Exception):
    """Raised after the final click when we cannot tell whether an order was placed."""


class KillSwitchEngaged(Exception):
    """Raised when the kill switch is present immediately before submission."""


@dataclass
class AttemptOutcome:
    event_id: str
    final_state: PurchaseState
    reason: str
    order_id: str | None = None
    dry_run: bool = False
    # Nothing was submitted and the failure was a page/network timing problem rather than a
    # policy refusal: the same offer may well succeed seconds later (rush conditions).
    transient: bool = False
