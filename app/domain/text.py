"""Normalización de texto y parsing determinista (sin LLM)."""

from __future__ import annotations

import re
import unicodedata

_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]")


def normalize_name(value: str) -> str:
    """'  Moisés  Pérez ' → 'moises perez'. Base de la búsqueda difusa (pg_trgm)."""
    decomposed = unicodedata.normalize("NFKD", value)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NON_WORD.sub(" ", no_accents.lower())
    return _SPACES.sub(" ", cleaned).strip()


def clean_display_name(value: str) -> str:
    value = _SPACES.sub(" ", value).strip()
    return value[:1].upper() + value[1:] if value else value


# ---- Confirmaciones (sí/no) ------------------------------------------------------------------------------------------
# Estricto a propósito: un falso "no entendí" solo cuesta repetir la respuesta, pero un falso "sí" ejecuta algo que el dueño
# no aprobó. Por eso NO se busca "sí" dentro de una frase: el mensaje ENTERO debe ser una respuesta conocida, o una palabra
# de respuesta seguida SOLO de rellenos de cortesía de una lista cerrada. Cualquier otra palabra ("pero", "cambia",
# un número, "y agrega…") lo saca de aquí y el mensaje sigue al LLM.
_AFFIRMATIVE = {
    "si", "sí", "sip", "sii", "siii", "dale", "ok", "okay", "okey", "va", "listo", "correcto", "claro",
    "confirmo", "confirmado", "de una", "hazlo", "adelante", "exacto", "eso", "perfecto", "afirmativo",
    "si dale", "si hazlo", "si correcto", "si confirmo", "si por favor", "bueno", "👍", "✅",
    "yes", "vale", "asi es", "eso es", "esta bien", "de acuerdo", "por supuesto", "claro que si", "que si",
}
_NEGATIVE = {
    "no", "nop", "nope", "nel", "cancela", "cancelar", "cancelalo", "no gracias", "dejalo", "déjalo",
    "olvidalo", "olvídalo", "mejor no", "no lo hagas", "negativo", "para", "no no", "❌",
}
# Primera palabra de una respuesta compuesta ("sí claro", "dale pues", "no, cancela") y lo único que puede acompañarla.
_YES_LEAD = {"si", "sip", "dale", "ok", "okay", "okey", "va", "listo", "claro", "confirmo", "confirmado", "hazlo", "adelante",
             "exacto", "perfecto", "correcto", "hecho", "yes", "vale", "bueno"}
_YES_FILLER = {"si", "claro", "senor", "senora", "pues", "gracias", "por", "favor", "porfa", "hazlo", "haz", "confirma",
               "confirmalo", "confirmo", "dale", "ya", "listo", "ok", "va", "vale", "perfecto", "adelante", "pana", "amigo",
               "bro", "epa"}
_NO_LEAD = {"no", "nop", "nope", "nel", "cancela", "cancelar", "cancelalo", "dejalo", "olvidalo", "negativo"}
_NO_FILLER = {"no", "gracias", "por", "favor", "porfa", "pues", "senor", "senora", "cancela", "cancelar", "cancelalo",
              "dejalo", "olvidalo", "mejor", "lo", "hagas", "pana", "amigo", "bro"}

_EMOJI_YES = {"👍", "✅", "👌"}
_EMOJI_NO = {"❌", "👎"}
# El tono de piel y el selector de variación no cambian el significado: "👍🏻" y "👍️" son el mismo pulgar.
_EMOJI_MODIFIERS = re.compile(r"[\U0001F3FB-\U0001F3FF\uFE0F\u200D]")
_AFFIRMATIVE_N = {normalize_name(a) for a in _AFFIRMATIVE if normalize_name(a)}
_NEGATIVE_N = {normalize_name(a) for a in _NEGATIVE if normalize_name(a)}
_STRETCHED = re.compile(r"(.)\1{2,}")  # "nooo" → "no", "siii" → "si", "daleee" → "dale"


def emoji_only(text: str) -> str | None:
    """Emoji base si el mensaje es SOLO ese emoji, con tono de piel o repetido ("👍🏻👍" → "👍"); si no, None."""
    stripped = _EMOJI_MODIFIERS.sub("", "".join(text.split()))
    if not stripped or len(set(stripped)) != 1 or stripped[0].isalnum():
        return None
    return stripped[0]


def _classify_short(text: str) -> str | None:
    raw = text.strip()
    emoji = emoji_only(raw)
    if emoji in _EMOJI_YES:
        return "yes"
    if emoji in _EMOJI_NO:
        return "no"
    if "?" in raw or "¿" in raw:
        return None  # una pregunta ("¿sí?", "si?") no es una respuesta: el dueño está dudando
    t = _STRETCHED.sub(r"\1", normalize_name(raw))
    words = t.split()
    if not words or len(words) > 5:
        return None
    if t in _AFFIRMATIVE_N:
        return "yes"
    if t in _NEGATIVE_N:
        return "no"
    if words[0] in _YES_LEAD and all(w in _YES_FILLER for w in words[1:]):
        return "yes"
    if words[0] in _NO_LEAD and all(w in _NO_FILLER for w in words[1:]):
        return "no"
    return None


def is_affirmative(text: str) -> bool:
    return _classify_short(text) == "yes"


def is_negative(text: str) -> bool:
    return _classify_short(text) == "no"


# ---- Acuses y despedidas ---------------------------------------------------------------------------------------------
# Un acuse NO pide nada: el dueño cierra el tema ("ok", "listo gracias", "chao", "👍"). Bolos lo acusa con una reacción
# 👍 en vez de gastar un mensaje. Mismo criterio estricto que el sí/no —vocabulario cerrado, sin cifras ni preguntas—:
# cualquier palabra de más ("ok y cuánto llevo", "listo, cámbialo") lo saca de aquí y el mensaje sigue al LLM.
_ACK_LEAD = {"ok", "oka", "okay", "okey", "oki", "okis", "dale", "listo", "perfecto", "excelente", "genial", "chevere",
             "fino", "bueno", "buenisimo", "vale", "va", "entendido", "entiendo", "correcto", "exacto", "claro",
             "gracias", "muchas", "mil", "chao", "chau", "adios", "bye", "hasta", "nos", "hablamos", "ah", "de", "esta",
             "muy", "asi"}
_ACK_FILLER = _ACK_LEAD | {"pues", "pana", "amigo", "bro", "epa", "hermano", "socio", "senor", "senora", "ya", "luego",
                           "manana", "vemos", "acuerdo", "bien", "veo", "por", "favor", "porfa", "igual", "igualmente",
                           "un", "abrazo", "saludos", "mas", "tarde", "es", "entonces", "anotado", "nada"}
_ACK_EMOJI = {"👍", "👌", "✅", "🙏", "🙌", "👏", "💪", "🤝"}


def is_acknowledgement(text: str) -> bool:
    """¿El mensaje es solo un acuse o una despedida, sin nada que responder? ("ok", "listo gracias", "chao", "👍🏻")."""
    raw = text.strip()
    if not raw:
        return False
    if (emoji := emoji_only(raw)) is not None:
        return emoji in _ACK_EMOJI
    if "?" in raw or "¿" in raw:
        return False  # una pregunta nunca es un acuse
    t = _STRETCHED.sub(r"\1", normalize_name(raw))
    words = t.split()
    if not words or len(words) > 5 or any(ch.isdigit() for ch in t):
        return False
    return words[0] in _ACK_LEAD and all(w in _ACK_FILLER for w in words[1:])


# Los comandos explícitos abren el catálogo; las preguntas de capacidades las interpreta el agente.
MENU_WORDS = {"menu", "menú", "ayuda", "help", "comandos"}


def is_menu_request(text: str) -> bool:
    return normalize_name(text) in {normalize_name(w) for w in MENU_WORDS}


_NUMBER = re.compile(r"(?<![\w.,])[-+]?\d[\d.,]*")


def parse_number_token(token: str) -> float | None:
    """Interpreta '1.234,56', '1,234.56', '1234.5', '14,50', '1.500' (miles) → float."""
    t = token.strip().lstrip("+")
    if not t or not any(ch.isdigit() for ch in t):
        return None
    t = t.rstrip(".,")
    has_dot, has_comma = "." in t, "," in t
    try:
        if has_dot and has_comma:
            if t.rfind(",") > t.rfind("."):
                return float(t.replace(".", "").replace(",", "."))
            return float(t.replace(",", ""))
        if has_comma:
            parts = t.split(",")
            if len(parts) == 2 and len(parts[1]) != 3:
                return float(t.replace(",", "."))
            return float(t.replace(",", ""))
        if has_dot:
            parts = t.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and parts[0] != "0"):
                # '1.500' en Venezuela es mil quinientos
                return float(t.replace(".", ""))
            return float(t)
        return float(t)
    except ValueError:
        return None


def extract_numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUMBER.finditer(text):
        value = parse_number_token(m.group())
        if value is not None:
            out.append(abs(value))
    return out


def optional_text(value: str | None) -> str | None:
    """Texto opcional para presentación; omite centinelas técnicos de entradas antiguas."""
    text = value.strip() if value else ""
    return text if text and text.casefold() not in {"null", "none", "undefined"} else None
