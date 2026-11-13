"""Verificación del origen de Bs/tasa en el límite entre el chat y los comandos."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.agent.deps import TurnDeps
from app.application.commands.brokerage import BrokerCommand
from app.application.money_evidence import source_spans_overlap, validate_quoted_money_source
from app.domain.brokerage import parse_broker_decimal

MONETARY_INPUT_FIELDS = ("operation_amount_bs", "exchange_rate")


@dataclass(frozen=True)
class MoneyEvidenceResult:
    """Resultado de preparar el comando; no representa una operación ejecutada.

    prepared_command: copia con las fuentes verificadas y el origen asignado por el sistema.
    unverified_fields: campo → motivo por el que NO se autoriza ese dato monetario.
    """

    prepared_command: BrokerCommand
    unverified_fields: dict[str, str] = field(default_factory=dict)

    @property
    def needs_money_evidence(self) -> bool:
        """True impide escribir: falta respaldo o la extracción no es utilizable."""
        return bool(self.unverified_fields)


def validate_required_money_evidence(
    command: BrokerCommand,
    deps: TurnDeps,
    tool_arguments: dict[str, Any],
    *,
    is_draft_continuation: bool = False,
) -> MoneyEvidenceResult:
    """Comprueba de dónde salen los Bs y la tasa propuestos por el modelo.

    No ejecuta el comando ni comprueba visualmente la imagen. Acepta:
    - un fragmento literal y único del mensaje actual que contiene el valor propuesto;
    - una extracción clara vinculada a una imagen que el pipeline entregó al modelo;
    - al continuar un borrador, una fuente que el servidor ya verificó y conservó durante una elección.

    La bandera monetary_from_attachment del modelo no concede autoridad. Esta función la reemplaza
    según el respaldo encontrado. Devuelve una copia del comando y los motivos de rechazo por campo.
    El llamador debe revisar needs_money_evidence ANTES de enviar el comando al CommandBus:
    las altas sin respaldo quedan en borrador; las correcciones se rechazan sin tocar el movimiento.
    """
    # Solo comprobamos datos aportados. Los importes derivados los calcula después el backend.
    proposed_money_values = {
        field_name: getattr(command, field_name)
        for field_name in MONETARY_INPUT_FIELDS
        if field_name in command.model_fields_set and getattr(command, field_name, None) is not None
    }
    receipt_extraction = tool_arguments.get("extraction")
    verified_money_sources: dict[str, dict] = {}
    command_changes: dict[str, Any] = {}
    unverified_fields: dict[str, str] = {}

    # Una imagen ilegible sin importe también puede adjuntarse como soporte de un registro existente.
    # Solo intentamos autorizar dinero si se propuso un valor o una extracción declarada clara.
    if receipt_extraction is not None and (
        proposed_money_values or receipt_extraction.get("confidence") == "clear"
    ):
        receipt_upload_id = getattr(command, "attachment_upload_id", None)
        available_receipt_ids = {deps.attachment_upload_id, *deps.preceding_receipt_ids} - {None}
        extracted_bolivars = receipt_extraction.get("amount_bs")

        # Una extracción debe referirse a bytes realmente disponibles, no a una imagen inventada/histórica.
        if receipt_upload_id not in available_receipt_ids:
            return MoneyEvidenceResult(
                command, {"operation_amount_bs": "El comprobante no está disponible en este turno."}
            )
        # La confianza viene del lector de imágenes. Ambiguous/unreadable nunca autoriza este importe.
        if receipt_extraction.get("confidence") != "clear" or extracted_bolivars is None:
            return MoneyEvidenceResult(
                command, {"operation_amount_bs": "La extracción no identifica un importe claro."}
            )

        extracted_bolivars = parse_broker_decimal(extracted_bolivars)
        # No puede haber dos importes distintos: uno en la extracción y otro en el comando.
        proposed_bolivars = proposed_money_values.get("operation_amount_bs")
        if extracted_bolivars <= 0 or (
            proposed_bolivars is not None and proposed_bolivars != extracted_bolivars
        ):
            return MoneyEvidenceResult(
                command, {"operation_amount_bs": "El importe es inválido o contradice la extracción."}
            )

        command_changes["operation_amount_bs"] = extracted_bolivars
        if "monetary_from_attachment" in type(command).model_fields:
            command_changes["monetary_from_attachment"] = True
        verified_money_sources["operation_amount_bs"] = {
            "value": str(extracted_bolivars),
            "source": "attachment",
            "attachment_id": str(receipt_upload_id),
            "message_id": str(deps.inbound_id),
            "extraction": receipt_extraction,
        }
        # Los Bs ya quedaron respaldados por la imagen; la tasa, si se propuso, aún debe comprobarse.
        proposed_money_values.pop("operation_amount_bs", None)
    elif "monetary_from_attachment" in type(command).model_fields:
        # Tener esta bandera en true no prueba nada: sin extracción, verificamos contra texto.
        command_changes["monetary_from_attachment"] = False

    text_source_quotes = tool_arguments.get("text_money_sources") or {}
    principal_usd = getattr(command, "operation_amount", None) or getattr(command, "amount", None)
    principal_source = None
    if principal_usd is not None and not is_draft_continuation and proposed_money_values:
        # Source roles are declared by the model; no position/keyword heuristic assigns a number to USD.
        principal_source = validate_quoted_money_source(
            deps.user_text,
            text_source_quotes.get("principal_usd"),
            Decimal(principal_usd),
            expected_currency="USD",
        )
        if principal_source is None:
            return MoneyEvidenceResult(
                command, {"principal_usd": "Cita el fragmento exacto que identifica los USD de la operación."}
            )
        verified_money_sources["operation_amount"] = {
            **principal_source,
            "source": "explicit",
            "message_id": str(deps.inbound_id),
        }
    used_text_sources = [principal_source] if principal_source else []
    for field_name, proposed_value in proposed_money_values.items():
        matching_text_span = validate_quoted_money_source(
            deps.user_text,
            text_source_quotes.get(field_name),
            Decimal(proposed_value),
        )
        if matching_text_span is not None and any(
            source_spans_overlap(matching_text_span, used) for used in used_text_sources
        ):
            matching_text_span = None
        if matching_text_span is not None:
            used_text_sources.append(matching_text_span)
            verified_money_sources[field_name] = {
                **matching_text_span,
                "source": "explicit",
                "message_id": str(deps.inbound_id),
            }
            continue

        # Durante una elección se conserva evidencia verificada en el servidor. No es historial libre
        # ni una declaración del modelo: solo la continuación del mismo borrador puede reutilizarla.
        previously_verified_source = (
            command.money_evidence.get(field_name, {}) if is_draft_continuation else {}
        )
        if previously_verified_source.get("value") == str(proposed_value) and previously_verified_source.get(
            "message_id"
        ):
            verified_money_sources[field_name] = previously_verified_source
            if (
                previously_verified_source.get("attachment_id")
                and "monetary_from_attachment" in type(command).model_fields
            ):
                command_changes["monetary_from_attachment"] = True
        else:
            unverified_fields[field_name] = (
                "El valor no tiene respaldo en el mensaje actual ni en la continuación verificada."
            )

    command_changes["money_evidence"] = verified_money_sources
    return MoneyEvidenceResult(command.model_copy(update=command_changes), unverified_fields)
