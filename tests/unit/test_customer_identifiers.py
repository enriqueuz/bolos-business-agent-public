import pytest

from app.domain.customers import is_nickname_reference, normalize_nickname, suggested_nickname


@pytest.mark.parametrize(("name", "nickname"), [
    ("Julia Example", "JUL"), ("Carlos Rodríguez", "CAR"), ("María Fernanda Pérez", "MAR"),
    ("José González", "JOS"), ("Alejandro Pérez", "ALE"), ("Al", None), (None, None),
])
def test_first_name_suggestion(name, nickname):
    assert suggested_nickname(name) == nickname


def test_normalization_and_three_letters_only_apply_to_a_customer_reference():
    assert normalize_nickname("  Mí   Apódo  ") == "MI APODO"
    assert is_nickname_reference("  júl ")
    assert not is_nickname_reference("compré 100 USD")
    assert not is_nickname_reference("jul pagó")
    assert not is_nickname_reference("Y0J")
