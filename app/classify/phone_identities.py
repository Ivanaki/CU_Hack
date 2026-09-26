"""Номера, которые человек явно связал с собой или знакомым."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PhoneIdentity, RawTransaction


class PhoneRejected(ValueError):
    pass


_PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s().-]*(?:\d[\s().-]*){10}(?!\d)")
_TRANSFER = re.compile(
    r"перевод\w*|сбп|зачислени\w*|поступлени\w*|отправлен\w*|"
    r"получен\w*\s+перевод|p2p|c2c|card2card",
    re.IGNORECASE,
)
_NOT_TRANSFER = re.compile(
    r"оплат\w*\s+(?:мобильн\w*\s+)?связ\w*|пополнени\w*\s+(?:мобильн\w*\s+)?телефона|"
    r"покупк\w*|магазин\w*|чек\s*№",
    re.IGNORECASE,
)


def normalize_phone(value: str) -> str:
    """Принимаем +7, 8 или 10 цифр; храним один полный российский номер."""
    raw = (value or "").strip()
    if not re.fullmatch(r"[+\d\s().-]+", raw):
        raise PhoneRejected("Укажите российский номер целиком")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("79"):
        raise PhoneRejected("Укажите российский мобильный номер целиком")
    return "+" + digits


def transfer_phone(row: RawTransaction) -> str | None:
    """Номер именно в реквизитах денежного перевода, а не в оплате связи."""
    text = f"{row.counterparty_raw or ''} {row.description or ''}"
    if row.mcc or not _TRANSFER.search(text) or _NOT_TRANSFER.search(text):
        return None
    found = set()
    for match in _PHONE.finditer(text):
        try:
            found.add(normalize_phone(match.group()))
        except PhoneRejected:
            continue
    return next(iter(found)) if len(found) == 1 else None


def identities(session: Session, user_id: int) -> dict[str, PhoneIdentity]:
    return {
        line.phone: line
        for line in session.scalars(
            select(PhoneIdentity).where(PhoneIdentity.user_id == user_id)
        ).all()
    }


def save_identity(
    session: Session, user_id: int, phone: str, kind: str, name: str = ""
) -> PhoneIdentity:
    number = normalize_phone(phone)
    if kind not in {"own", "contact"}:
        raise PhoneRejected("Выберите «мой номер» или «человек»")
    name = " ".join((name or "").split())
    if kind == "contact" and (not name or len(name) > 100):
        raise PhoneRejected("Укажите имя человека до 100 символов")
    if kind == "own":
        name = ""
    line = session.scalar(select(PhoneIdentity).where(
        PhoneIdentity.user_id == user_id, PhoneIdentity.phone == number
    ))
    if line is not None and line.kind != kind:
        raise PhoneRejected("Номер уже сохранён в другой роли. Сначала удалите прежнюю запись")
    if line is None:
        line = PhoneIdentity(user_id=user_id, phone=number, kind=kind, name=name)
        session.add(line)
    else:
        line.name = name
    session.flush()
    return line


def delete_identity(session: Session, user_id: int, phone: str) -> bool:
    number = normalize_phone(phone)
    line = session.scalar(select(PhoneIdentity).where(
        PhoneIdentity.user_id == user_id, PhoneIdentity.phone == number
    ))
    if line is None:
        return False
    session.delete(line)
    session.flush()
    return True
