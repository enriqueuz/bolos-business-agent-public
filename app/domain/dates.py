"""Fechas de negocio y períodos. Toda consulta filtra por `business_date` local, nunca por UTC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.domain.errors import ValidationFailed

WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


class PeriodName(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    THIS_YEAR = "this_year"
    CUSTOM = "custom"


class CompareTo(StrEnum):
    PREVIOUS_PERIOD = "previous_period"
    SAME_PERIOD_LAST_YEAR = "same_period_last_year"


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date  # inclusivo

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def label(self) -> str:
        if self.start == self.end:
            return fmt_date(self.start)
        return f"del {fmt_date(self.start)} al {fmt_date(self.end)}"

    def to_dict(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "label": self.label()}


def local_now(tz: str, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or datetime.now(UTC)
    return now_utc.astimezone(ZoneInfo(tz))


def local_today(tz: str, now_utc: datetime | None = None) -> date:
    return local_now(tz, now_utc).date()


def occurred_at_for(business_date: date, tz: str, now_utc: datetime) -> datetime:
    """Si el registro es de hoy, usa la hora actual; si es de otro día, mediodía local de ese día."""
    if business_date == local_today(tz, now_utc):
        return now_utc
    return datetime.combine(business_date, time(12, 0), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def validate_business_date(d: date | None, tz: str, now_utc: datetime) -> date:
    today = local_today(tz, now_utc)
    if d is None:
        return today
    if d > today:
        raise ValidationFailed("La fecha no puede ser futura.", date=d.isoformat(), today=today.isoformat())
    if (today - d).days > 400:
        raise ValidationFailed("La fecha es demasiado antigua (más de 400 días).", date=d.isoformat())
    return d


def resolve_period(
    period: PeriodName, today: date, start: date | None = None, end: date | None = None
) -> DateRange:
    monday = today - timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)
    match period:
        case PeriodName.TODAY:
            return DateRange(today, today)
        case PeriodName.YESTERDAY:
            y = today - timedelta(days=1)
            return DateRange(y, y)
        case PeriodName.THIS_WEEK:
            return DateRange(monday, today)
        case PeriodName.LAST_WEEK:
            return DateRange(monday - timedelta(days=7), monday - timedelta(days=1))
        case PeriodName.THIS_MONTH:
            return DateRange(first_of_month, today)
        case PeriodName.LAST_MONTH:
            last_end = first_of_month - timedelta(days=1)
            return DateRange(last_end.replace(day=1), last_end)
        case PeriodName.LAST_7_DAYS:
            return DateRange(today - timedelta(days=6), today)
        case PeriodName.LAST_30_DAYS:
            return DateRange(today - timedelta(days=29), today)
        case PeriodName.THIS_YEAR:
            return DateRange(today.replace(month=1, day=1), today)
        case PeriodName.CUSTOM:
            if not start:
                raise ValidationFailed("Para un período personalizado indica al menos la fecha inicial (from).")
            end = end or start
            if end < start:
                start, end = end, start
            if (end - start).days > 800:
                raise ValidationFailed("El período no puede superar ~2 años.")
            return DateRange(start, min(end, today))
    raise ValidationFailed(f"Período desconocido: {period}")


def comparison_range(rng: DateRange, compare_to: CompareTo) -> DateRange:
    if compare_to == CompareTo.PREVIOUS_PERIOD:
        end = rng.start - timedelta(days=1)
        return DateRange(end - timedelta(days=rng.days - 1), end)

    def shift(d: date) -> date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:  # 29 de febrero
            return d.replace(year=d.year - 1, day=28)

    return DateRange(shift(rng.start), shift(rng.end))


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS_ES[d.weekday()]} {d.day} de {MONTHS_ES[d.month - 1]}"


def fmt_short_date(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def days_ago_label(d: date, today: date) -> str:
    delta = (today - d).days
    if delta <= 0:
        return "hoy"
    if delta == 1:
        return "ayer"
    return f"hace {delta} días"
