from datetime import date

import pytest

from app.application.reconciliation import historical_date
from app.domain.errors import ValidationFailed


def test_date_policy():
    today = date(2026, 11, 4)
    assert historical_date(31, 10, None, today) == date(2026, 10, 31)
    assert historical_date(1, 1, 2020, today) == date(2020, 1, 1)
    with pytest.raises(ValidationFailed, match="año"):
        historical_date(31, 12, None, today)
    with pytest.raises(ValidationFailed):
        historical_date(31, 2, 2026, today)
    with pytest.raises(ValidationFailed, match="futura"):
        historical_date(31, 12, 2026, today)
