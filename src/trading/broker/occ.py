"""OCC option symbol construction and parsing.

OCC format: <ROOT><YYMMDD><C|P><STRIKE*1000 zero-padded to 8 digits>
e.g. AAPL  260116C00190000  ->  AAPL 2026-01-16 call $190.00
The root is left-justified; strike is in thousandths of a dollar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class OccParts:
    underlying: str
    expiry: date
    right: str          # 'call' | 'put'
    strike: float


def build_occ(underlying: str, expiry: date, right: str, strike: float) -> str:
    if right not in ("call", "put"):
        raise ValueError(f"right must be call|put, got {right!r}")
    root = underlying.upper()
    ymd = expiry.strftime("%y%m%d")
    cp = "C" if right == "call" else "P"
    strike_int = round(strike * 1000)
    if strike_int < 0 or strike_int > 99_999_999:
        raise ValueError(f"strike {strike} out of OCC range")
    return f"{root}{ymd}{cp}{strike_int:08d}"


def parse_occ(occ: str, underlying: str | None = None) -> OccParts:
    """Parse an OCC symbol. If `underlying` is given it's stripped as the root;
    otherwise the root is inferred as the leading alphabetic characters before
    the 6-digit date."""
    occ = occ.strip().upper()
    if underlying:
        root = underlying.upper()
        tail = occ[len(root):]
    else:
        i = 0
        while i < len(occ) and occ[i].isalpha():
            i += 1
        # date is 6 digits; the root ends where the 6-digit date begins
        i = len(occ) - 15
        root = occ[:i]
        tail = occ[i:]
    if len(tail) != 15 or tail[6] not in ("C", "P"):
        raise ValueError(f"malformed OCC symbol: {occ!r}")
    expiry = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
    right = "call" if tail[6] == "C" else "put"
    strike = int(tail[7:]) / 1000.0
    return OccParts(underlying=root, expiry=expiry, right=right, strike=strike)
