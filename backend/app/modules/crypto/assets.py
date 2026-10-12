"""Crypto instruments and the fiat currencies that are booked as cash."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument

FIAT = {"EUR", "USD", "GBP", "SEK", "NOK", "DKK", "CHF", "CAD", "AUD", "JPY", "PLN", "CZK"}


def crypto_instrument(db: Session, symbol: str) -> Instrument:
    inst = db.scalar(select(Instrument).where(Instrument.type == "crypto", Instrument.symbol == symbol))
    if inst is None:
        inst = Instrument(
            type="crypto",
            name=symbol,
            symbol=symbol,
            currency="EUR",
            price_symbol=f"{symbol}-EUR",
            asset_class="crypto",
        )
        db.add(inst)
        db.flush()
    return inst
