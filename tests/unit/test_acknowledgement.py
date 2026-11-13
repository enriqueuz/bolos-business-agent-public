"""El acuse: lo que Bolos puede responder con un 👍 en vez de un mensaje.

Mismo criterio estricto que el sí/no. Un falso acuse es peor que un falso "no entendí": el dueño pidió algo, Bolos
le puso 👍 y no hizo nada. Por eso cualquier palabra fuera del vocabulario cerrado, cualquier cifra y cualquier
pregunta sacan al mensaje de aquí y lo mandan al LLM.
"""

import pytest

from app.domain.text import emoji_only, is_acknowledgement, is_affirmative, is_negative

ACK = [
    # acuses secos
    "ok", "Ok", "OK!", "okey", "oki", "listo", "dale", "dale pues", "vale", "perfecto", "excelente", "genial",
    "chévere", "fino", "bueno", "entendido", "entiendo", "correcto", "claro", "así es", "de acuerdo", "está bien",
    "muy bien", "ah ok", "ah ya", "ah bueno",
    # agradecimientos y cierres
    "gracias", "Gracias!", "muchas gracias", "mil gracias", "ok gracias", "listo gracias", "perfecto gracias",
    "entendido, gracias", "gracias pana", "ok, gracias por favor",
    # despedidas
    "chao", "chau", "adiós", "bye", "hasta luego", "hasta mañana", "nos vemos", "hablamos luego",
    # emojis (con tono de piel y repetidos: son el mismo pulgar)
    "👍", "👍🏻", "👍🏿", "👍👍", "👌", "✅", "🙏", "🙌", "ok 👍",
]
# Nada de esto se acusa con 👍: o pide algo, o pregunta, o trae datos que hay que registrar.
NOT_ACK = [
    # pide algo además del acuse
    "ok pero cámbialo a 20", "listo, vendí 3 bujías", "ok y agrega 5$ de luz", "dale hazlo", "ok borra eso",
    "gracias, pero eran 2", "listo el pago de Moisés",
    # preguntas
    "ok?", "¿listo?", "gracias, ¿cuánto vendí hoy?", "¿y entonces?",
    # saludos y otros mensajes (necesitan respuesta de verdad)
    "hola", "buenas noches", "buen día", "sí", "no", "vendí 2 bujías a 3$", "cuánto llevo hoy",
    # vacíos, ruido y palabrería
    "", "   ", "ok " * 6, "❤️", "😂", "👍👎",
]


@pytest.mark.parametrize("text", ACK)
def test_recognizes_an_acknowledgement(text):
    assert is_acknowledgement(text), text


@pytest.mark.parametrize("text", NOT_ACK)
def test_everything_else_needs_a_real_answer(text):
    assert not is_acknowledgement(text), text


@pytest.mark.parametrize("text", ["👍🏽", "👍🏻", "👍👍", "👍️"])
def test_a_thumbs_up_confirms_whatever_its_skin_tone(text):
    """El dueño manda 👍🏽 a una confirmación pendiente: es un sí, como el 👍 amarillo."""
    assert is_affirmative(text) and is_acknowledgement(text)


def test_emoji_only_ignores_modifiers_and_repetition():
    assert emoji_only("👍🏻") == "👍" and emoji_only("👍 👍") == "👍"
    assert emoji_only("ok") is None and emoji_only("👍👌") is None and emoji_only("") is None


def test_a_thumbs_down_is_still_a_no_and_never_an_acknowledgement():
    assert is_negative("👎🏾") and not is_acknowledgement("👎")
