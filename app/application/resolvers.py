"""Resolución difusa de nombres (clientes y productos) con pg_trgm.

El dueño nunca dice ids: dice "moises", "la cadena 428". Aquí se decide si es un registro existente,
si hay ambigüedad (hay que preguntar) o si es nuevo.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.customer_identity import create_customer, lock_customer_identity
from app.db.models import Customer, Product
from app.domain.customers import is_nickname_reference, normalize_nickname
from app.domain.errors import Ambiguous, NotFound, ValidationFailed
from app.domain.text import clean_display_name, normalize_name

STRONG = 0.72  # por encima: es el mismo
WEAK = 0.34  # entre WEAK y STRONG: zona gris → preguntar
EXACT = 2.0  # marcador de coincidencia exacta normalizada (fuera del rango 0..1 de pg_trgm)

# Conectores gramaticales del idioma principal del producto. No describen rubros
# ni tipos de mercancía: solo permiten comparar "sensor cambio" con "sensor de
# cambio". La identidad se decide con cobertura entre nombres, no con un
# vocabulario de repuestos, comida, ropa u otra vertical.
_NAME_CONNECTORS = frozenset({"de", "del", "la", "el", "los", "las", "para", "por", "con"})


@dataclass(frozen=True, slots=True)
class Match[T]:
    entity: T
    score: float


async def _candidates(session: AsyncSession, model: type, business_id: UUID, name: str, limit: int = 5, *, visibility=None) -> list[Match]:
    norm = normalize_name(name)
    if not norm:
        raise ValidationFailed("El nombre está vacío.")
    score = func.greatest(
        func.similarity(model.normalized_name, norm),
        func.word_similarity(norm, model.normalized_name),
    )
    rows = (
        await session.execute(
            select(model, score.label("score"))
            .where(model.business_id == business_id, model.deleted_at.is_(None), score >= WEAK, *([visibility] if visibility is not None else []))
            .order_by(score.desc(), func.length(model.normalized_name))
            .limit(limit)
        )
    ).all()
    matches = [Match(r[0], float(r[1])) for r in rows]
    # coincidencia exacta normalizada siempre gana
    exact = [m for m in matches if m.entity.normalized_name == norm]
    if exact:
        # No elegir arbitrariamente el primer homónimo. Los dos pueden ser personas
        # reales y la capa de interacción debe pedir cuál es.
        return [Match(m.entity, EXACT) for m in exact] + [
            m for m in matches if not any(m.entity is selected.entity for selected in exact)
        ]
    return matches


def _product_tokens(name: str) -> list[str]:
    return [token for token in normalize_name(name).split() if token not in _NAME_CONNECTORS]


def _near_token(left: str, right: str) -> bool:
    """Tolera una errata corta (Mobil/Movil), pero no confunde códigos distintos (ABC/DEF)."""
    if left == right:
        return True
    # Medidas, modelos y SKUs con cifras son identidad, no prosa: 260926 y
    # 260927 pueden ser dos productos legítimos aunque difieran en un carácter.
    if any(char.isdigit() for char in left + right):
        return False
    if min(len(left), len(right)) < 4:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.78


def _aligned_token_count(left: list[str], right: list[str]) -> int:
    """Máxima cobertura uno-a-uno entre dos nombres, sin asumir orden ni rubro.

    Primero consume tokens exactos y después tolera erratas de prosa. Un token no
    puede justificar dos partes del otro nombre. Esto permite comparar tanto
    ``Apple iPhone 15`` / ``iPhone 15 Apple`` como nombres en orden natural.
    """
    available = set(range(len(right)))
    matched = 0
    for token in left:
        exact = next((i for i in available if token == right[i]), None)
        if exact is not None:
            available.remove(exact)
            matched += 1
            continue
        near = next((i for i in available if _near_token(token, right[i])), None)
        if near is not None:
            available.remove(near)
            matched += 1
    return matched


def _name_coverage(query: str, candidate: str) -> tuple[int, int, int]:
    query_tokens, candidate_tokens = _product_tokens(query), _product_tokens(candidate)
    return _aligned_token_count(query_tokens, candidate_tokens), len(query_tokens), len(candidate_tokens)


def _product_candidate_is_relevant(query: str, candidate: str) -> bool:
    """Exige que uno de los nombres explique casi todo el otro.

    pg_trgm recupera candidatos; esta barrera neutral al rubro decide si vale la
    pena mostrarlos. Compartir solo ``QA260926``, ``kit``, ``combo`` o cualquier
    otra palabra no alcanza: al menos 75% de uno de los dos nombres debe quedar
    explicado por el otro. No importa cuál palabra sea genérica en ese negocio.
    """
    matched, query_len, candidate_len = _name_coverage(query, candidate)
    if not query_len or not candidate_len:
        return False
    return matched / query_len >= 0.75 or matched / candidate_len >= 0.75


def _has_distinct_modifier(query: str, candidate: str) -> bool:
    """¿El nombre dado contiene identidad que el candidato no explica?

    Con precio/costo explícito, cualquier token identificador adicional basta
    para tratarlo como producto nuevo: ABC/DEF, 500 ml/1 L, rojo/azul,
    básico/premium, etc. No hay listas por industria.
    """
    matched, query_len, _ = _name_coverage(query, candidate)
    return matched < query_len


def _query_is_underspecified(query: str, candidate: str) -> bool:
    matched, query_len, candidate_len = _name_coverage(query, candidate)
    return bool(query_len) and matched == query_len and candidate_len > query_len


def _pick(matches: list[Match], label: str, name: str, new_hint: str | None = None, *, allow_fuzzy: bool = True) -> Match | None:
    if not matches:
        return None
    best = matches[0]
    exact = [m for m in matches if m.score >= EXACT]
    if len(exact) == 1:
        return best
    if len(exact) > 1:
        raise Ambiguous(
            f"Hay varios {label}s llamados '{name}'.",
            entity=label,
            name=name,
            candidates=[m.entity.display_name if isinstance(m.entity, Customer) else m.entity.name for m in exact[:5]],
            candidate_ids=[str(m.entity.id) for m in exact[:5]],
            candidate_descriptions=[_customer_hint(m.entity) if isinstance(m.entity, Customer) else None for m in exact[:5]],
            hint="Indica cuál es o aclara que es una persona nueva.",
        )
    runner_up = matches[1].score if len(matches) > 1 else 0.0
    if allow_fuzzy and best.score >= STRONG and best.score - runner_up >= 0.12:
        return best
    raise Ambiguous(
        f"No estoy seguro de a qué {label} te refieres con '{name}'.",
        entity=label,
        name=name,
        candidates=[m.entity.display_name if isinstance(m.entity, Customer) else m.entity.name for m in matches[:5]],
        candidate_ids=[str(m.entity.id) for m in matches[:5]],
        candidate_descriptions=[_customer_hint(m.entity) if isinstance(m.entity, Customer) else None for m in matches[:5]],
        hint="Pregunta cuál es, o vuelve a llamar con el nombre exacto de un candidato." + (f" Y {new_hint}" if new_hint else ""),
    )


def _customer_hint(customer: Customer) -> str | None:
    """Información ya guardada para distinguir homónimos, sin inventar identidad."""
    scope = "Global" if customer.classification == "global" else (
        f"Personal · {customer.owner_broker.name}" if customer.classification == "personal" and customer.owner_broker else None)
    parts = [value for value in (customer.nickname, scope, customer.phone, customer.notes) if value]
    return " · ".join(parts) or None


def brokerage_customer_visibility(visible_broker_id: UUID | None):
    visibility = Customer.classification.is_not(None)
    if visible_broker_id:
        visibility = and_(visibility, or_(Customer.classification == "global",
                                       Customer.owner_broker_member_id == visible_broker_id))
    return visibility


async def search_brokerage_customers(session: AsyncSession, business_id: UUID, name: str,
                                    visible_broker_id: UUID | None, *, limit: int = 20) -> list[Customer]:
    """Suggestions only: a fuzzy result never authorizes selecting an identity."""
    norm = normalize_name(name)
    score = func.greatest(func.similarity(Customer.normalized_name, norm),
                          func.word_similarity(norm, Customer.normalized_name))
    statement = select(Customer).where(Customer.business_id == business_id, Customer.deleted_at.is_(None),
                                       brokerage_customer_visibility(visible_broker_id))
    if norm:
        statement = statement.where(or_(Customer.normalized_name.contains(norm, autoescape=True), Customer.nickname.contains(normalize_nickname(name), autoescape=True), score >= WEAK))
    return list((await session.execute(statement.order_by(
        (Customer.nickname == normalize_nickname(name)).desc(), (Customer.normalized_name == norm).desc(), score.desc(), Customer.name, Customer.id
    ).limit(limit))).scalars())


async def resolve_customer(
    session: AsyncSession,
    business_id: UUID,
    name: str | None,
    *,
    create_if_missing: bool,
    force_new: bool = False,
    customer_id: UUID | None = None,
    brokerage: bool = False,
    visible_broker_id: UUID | None = None,
    classification: str | None = None,
    owner_broker_member_id: UUID | None = None,
) -> tuple[Customer, bool]:
    """Devuelve (cliente, creado), filtering candidates before ambiguity is exposed."""
    visibility = None
    if brokerage:
        visibility = brokerage_customer_visibility(visible_broker_id)
        if classification is not None:
            visibility = and_(visibility, Customer.classification == classification)
    if customer_id is not None:
        customer = await session.get(Customer, customer_id)
        if customer is None or customer.business_id != business_id or customer.deleted_at is not None:
            raise NotFound("Cliente no encontrado.")
        if brokerage and (customer.classification is None or
                          visible_broker_id and customer.classification != "global" and customer.owner_broker_member_id != visible_broker_id):
            raise NotFound("Cliente no encontrado.")
        if brokerage and classification is not None and customer.classification != classification:
            raise ValidationFailed("El cliente seleccionado tiene otra clasificación; no se puede cambiar por una operación.")
        return customer, False
    if not name:
        raise ValidationFailed("Indica el cliente.")
    # All creation paths share the same lock, so a concurrent first use cannot create duplicates.
    if create_if_missing or force_new:
        await lock_customer_identity(session, business_id)
    visible = [visibility] if visibility is not None else []
    base = [Customer.business_id == business_id, Customer.deleted_at.is_(None), *visible]
    alias = (await session.execute(select(Customer).where(*base, Customer.nickname == normalize_nickname(name)))).scalar_one_or_none()
    if alias is not None:
        # Even an explicit "new customer" must not duplicate an existing nickname reference.
        return alias, False
    if not force_new:
        exact = (await session.execute(select(Customer).where(
            *base, Customer.normalized_name == normalize_name(name),
        ).order_by(Customer.id).limit(5))).scalars().all()
        matches = [Match(customer, EXACT) for customer in exact]
        if not matches:
            norm = normalize_name(name)
            partial = (await session.execute(select(Customer).where(
                *base, Customer.normalized_name.contains(norm, autoescape=True),
            ).order_by(Customer.id).limit(6))).scalars().all()
            # A unique partial name is deterministic; several always require clarification.
            if partial:
                matches = [Match(customer, EXACT) for customer in partial]
        if not matches:
            matches = await _candidates(session, Customer, business_id, name, visibility=visibility)
        match = _pick(matches, "cliente", name,
                      "si es un cliente nuevo, usa force_new_customer=true.", allow_fuzzy=not brokerage)
        if match:
            return match.entity, False
    if not create_if_missing and not force_new:
        raise NotFound(f"No encontré ningún cliente llamado '{name}'.")
    if brokerage and classification is None:
        raise NotFound("El cliente necesita clasificación.", classification_required=True)
    nickname_only = is_nickname_reference(name)
    customer = await create_customer(session, business_id, name=None if nickname_only else name,
                        nickname=normalize_nickname(name) if nickname_only else None,
                        classification=classification if brokerage else None,
                        owner_broker_member_id=owner_broker_member_id if brokerage and classification == "personal" else None)
    return customer, True


async def find_product(
    session: AsyncSession, business_id: UUID, name: str, *, distinct_name_is_new: bool = False
) -> Product | None:
    """Resuelve un producto existente sin mezclar familias ni variantes claras.

    ``distinct_name_is_new`` se activa en movimientos que ya traen precio/costo:
    ese dato, junto con un modificador de nombre distinto, basta para crear el
    producto sin una pregunta redundante.
    """
    matches = await _candidates(session, Product, business_id, name, limit=20)
    matches = [match for match in matches if _product_candidate_is_relevant(name, match.entity.name)]
    if distinct_name_is_new and matches and matches[0].score < EXACT:
        matches = [match for match in matches if not _has_distinct_modifier(name, match.entity.name)]
    # word_similarity("aceite", "aceite Mobil") puede ser 1.0, pero el nombre
    # corto no identifica la variante. Mantenerlo en zona gris obliga a elegir.
    matches = [Match(match.entity, min(match.score, STRONG - 0.01))
               if match.score < EXACT and _query_is_underspecified(name, match.entity.name) else match
               for match in matches]
    return m.entity if (m := _pick(matches, "producto", name,
                                  "si es un producto nuevo, usa un nombre más específico que lo distinga.")) else None


async def search_products(session: AsyncSession, business_id: UUID, query: str, limit: int = 5) -> list[Match]:
    matches = await _candidates(session, Product, business_id, query, max(limit * 4, 20))
    return [match for match in matches if _product_candidate_is_relevant(query, match.entity.name)][:limit]


async def get_or_create_product(
    session: AsyncSession,
    business_id: UUID,
    name: str,
    *,
    force_new: bool = False,
    distinct_name_is_new: bool = False,
) -> tuple[Product, bool]:
    product = None if force_new else await find_product(
        session, business_id, name, distinct_name_is_new=distinct_name_is_new
    )
    if product:
        return product, False
    product = Product(business_id=business_id, name=clean_display_name(name), normalized_name=normalize_name(name))
    session.add(product)
    await session.flush()
    return product, True
