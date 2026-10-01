"""Kalshi and PM US FIX session options for cancel-on-disconnect.

Kalshi: tag 8013 on Logon. Docs fetched 1 October 2026,
https://docs.kalshi.com/fix/authentication. Default N. Y cancels open
orders on disconnect, including a graceful logout. Listener sessions
must not set Y (https://docs.kalshi.com/fix/listener-sessions).

PM US: CancelOnDisconnect is a session-config flag, not a logon tag in
the page we fetched. It cancels DAY orders only; GTC and GTD remain
(https://docs.polymarket.us/institutional/fix-api/fix-session-management).

Neither constructor opens a socket. Both flags default False.
"""
from __future__ import annotations

KALSHI_FIX_CANCEL_ON_DISCONNECT = False
PMUS_FIX_CANCEL_ON_DISCONNECT = False

SOH = "\x01"
CANCEL_ON_DISCONNECT_TAG = 8013


class FixSessionError(ValueError):
    pass


def _checksum(body: str) -> str:
    total = sum(body.encode("ascii")) % 256
    return f"{total:03d}"


def encode_fix(fields: list[tuple[int, str]]) -> str:
    """FIX message with BodyLength (9) and CheckSum (10). Tag 8 is first, 35 third."""
    if not fields or fields[0][0] != 8:
        raise FixSessionError("BeginString must be first")
    rest = fields[1:]
    # Body is everything after BeginString, before checksum, including 9 and 35.
    # Compute length of the body that follows tag 9.
    without_length = [(t, v) for t, v in rest if t != 9]
    # 35 must be the first of the body.
    body_fields = without_length
    body = SOH.join(f"{t}={v}" for t, v in body_fields) + SOH
    length = len(body.encode("ascii"))
    head = f"8={fields[0][1]}{SOH}9={length}{SOH}"
    raw = head + body
    return raw + f"10={_checksum(raw)}{SOH}"


def parse_fix(raw: str) -> dict[int, str]:
    parts = raw.split(SOH)
    out: dict[int, str] = {}
    for part in parts:
        if not part or "=" not in part:
            continue
        tag, _, val = part.partition("=")
        out[int(tag)] = val
    return out


def kalshi_logon(*, cancel_on_disconnect: bool | None = None,
                 listener: bool = False,
                 heartbeat_int: int = 30,
                 signature_b64: str = "<signature>") -> str:
    """Logon (35=A). 8013 is N unless cancel-on-disconnect is requested.

    ``cancel_on_disconnect=None`` reads ``KALSHI_FIX_CANCEL_ON_DISCONNECT``,
    which defaults False. A listener session cannot combine with Y.
    """
    enabled = (KALSHI_FIX_CANCEL_ON_DISCONNECT
               if cancel_on_disconnect is None else bool(cancel_on_disconnect))
    if listener and enabled:
        raise FixSessionError(
            "listener session (20126=Y) must not set CancelOrdersOnDisconnect")
    if heartbeat_int < 3:
        raise FixSessionError("HeartbeatInt must be >= 3")
    fields: list[tuple[int, str]] = [
        (8, "FIXT.1.1"),
        (35, "A"),
        (98, "0"),
        (108, str(int(heartbeat_int))),
        (1137, "9"),
        (96, signature_b64),
        (8013, "Y" if enabled else "N"),
    ]
    if listener:
        fields.append((20126, "Y"))
        fields.append((21011, "Y"))
    return encode_fix(fields)


def pmus_fix_session(*, cancel_on_disconnect: bool | None = None) -> dict:
    """PM US FIX session config. Not a wire logon.

    Y cancels DAY orders on disconnect. GTC and GTD are not cancelled.
    """
    enabled = (PMUS_FIX_CANCEL_ON_DISCONNECT
               if cancel_on_disconnect is None else bool(cancel_on_disconnect))
    return {
        "CancelOnDisconnect": "Y" if enabled else "N",
        "cancels": "DAY" if enabled else "none",
        "leaves_resting": ["GTC", "GTD"],
    }


class MockKalshiFixAcceptor:
    """If logon had 8013=Y, a dropped transport cancels resting orders."""

    def __init__(self) -> None:
        self.cod = False
        self.orders: dict[str, str] = {}
        self.disconnected = False

    def on_logon(self, raw: str) -> None:
        tags = parse_fix(raw)
        if tags.get(35) != "A":
            raise FixSessionError("not a logon")
        if tags.get(8) != "FIXT.1.1":
            raise FixSessionError("BeginString")
        self.cod = tags.get(8013) == "Y"

    def place(self, order_id: str) -> None:
        self.orders[order_id] = "resting"

    def drop_socket(self) -> None:
        self.disconnected = True
        if self.cod:
            for oid in list(self.orders):
                self.orders[oid] = "canceled"


class MockPMUSFixGateway:
    """CancelOnDisconnect=Y cancels DAY orders and leaves GTC resting."""

    def __init__(self) -> None:
        self.cod = False
        self.orders: dict[str, str] = {}
        self.tif: dict[str, str] = {}

    def configure(self, session: dict) -> None:
        self.cod = session.get("CancelOnDisconnect") == "Y"

    def place(self, order_id: str, *, tif: str) -> None:
        self.orders[order_id] = "resting"
        self.tif[order_id] = tif

    def drop_socket(self) -> None:
        if not self.cod:
            return
        for oid, tif in self.tif.items():
            if tif == "DAY":
                self.orders[oid] = "canceled"
