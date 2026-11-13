"""Composition root: el ÚNICO lugar que conoce todas las implementaciones y las cablea.

Producción, worker, tests y scripts construyen un Container. Los tests reemplazan piezas (modelos, mensajería)
pasando overrides; nunca se parchean módulos.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.extractors import LlmOnboardingInterpreter, LlmTranscriber
from app.agent.orchestrator import AgentOrchestrator, PydanticAIOrchestrator
from app.agent.pipeline import TurnPipeline
from app.application.attachments import AttachmentService
from app.application.bus import CommandBus, CommandDeps, load_all_commands
from app.application.conversation import ConversationStore
from app.application.identity import IdentityService
from app.application.incidents import IncidentReporter
from app.application.onboarding import OnboardingFlow
from app.application.panel_links import PanelLinks
from app.application.ports import Alerter, RateFetcher
from app.application.queries.brokerage import BrokerageQueries
from app.application.queries.service import QueryService
from app.application.rates import RateService
from app.application.turns import TurnLog
from app.channels.console import ConsoleProvider
from app.channels.kapso.provider import KapsoProvider
from app.channels.port import MessagingProvider
from app.config import Settings
from app.db.session import make_engine, make_session_factory
from app.infra.alerts.telegram import TelegramAlerter
from app.infra.observability.capture import SentryIncidentReporter, configure_sentry
from app.infra.rates.dolarapi import build_rate_fetchers
from app.infra.redis.conversation_store import RedisConversationStore
from app.infra.storage.memory import MemoryAttachmentStore
from app.infra.storage.s3 import S3AttachmentStore
from app.llm.observability import log_effective_llm_config
from app.llm.recorder import validate_price_overrides
from app.llm.roles import ModelRoles, roles_from_settings


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    alerter: Alerter
    rates: RateService
    bus: CommandBus
    queries: QueryService
    brokerage_queries: BrokerageQueries
    turns: TurnLog
    store: ConversationStore
    identity: IdentityService
    messaging: MessagingProvider
    roles: ModelRoles
    orchestrator: AgentOrchestrator
    onboarding: OnboardingFlow
    attachments: AttachmentService
    pipeline: TurnPipeline
    links: PanelLinks
    incidents: IncidentReporter

    async def close(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()


def build_messaging(settings: Settings) -> MessagingProvider:
    if settings.messaging_provider == "kapso":
        return KapsoProvider(api_key=settings.kapso_api_key or "", phone_number_id=settings.kapso_phone_number_id or "",
                             webhook_secret=settings.kapso_webhook_secret, base_url=settings.kapso_base_url,
                             api_version=settings.kapso_api_version, platform_base_url=settings.kapso_platform_base_url,
                             additional_numbers={number_id: (number.api_key or settings.kapso_api_key or "", number.webhook_secret)
                                                 for number_id, number in settings.kapso_additional_numbers.items()})
    return ConsoleProvider()


def build_container(settings: Settings, *, messaging: MessagingProvider | None = None, roles: ModelRoles | None = None,
                    rate_fetchers: dict[str, RateFetcher] | None = None, alerter: Alerter | None = None) -> Container:
    configure_sentry(settings)
    load_all_commands()
    validate_price_overrides(settings.llm_price_overrides)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    alerter = alerter or TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
    rates = RateService(factory, rate_fetchers if rate_fetchers is not None else build_rate_fetchers(), alerter)
    bus = CommandBus(factory, CommandDeps(rates=rates))
    queries = QueryService(factory, rates)
    brokerage_queries = BrokerageQueries(factory)
    turns = TurnLog(factory)
    incidents = SentryIncidentReporter(settings, turns, alerter)
    store = RedisConversationStore(redis)
    identity = IdentityService(factory, signup_mode=settings.signup_mode, allowed_phones=settings.allowed_phones,
                               default_country=settings.default_country)
    messaging = messaging or build_messaging(settings)
    roles = roles or roles_from_settings(settings)
    log_effective_llm_config(settings, roles)
    orchestrator = PydanticAIOrchestrator(roles, agent_request_limit=settings.agent_request_limit,
                                          fast_request_limit=settings.fast_request_limit, price_overrides=settings.llm_price_overrides,
                                          agent_thinking=settings.llm_agent_thinking, fast_thinking=settings.llm_fast_thinking,
                                          request_timeout_s=settings.llm_request_timeout_s)
    interpreter = LlmOnboardingInterpreter(roles, turns, price_overrides=settings.llm_price_overrides,
                                          request_timeout_s=settings.llm_request_timeout_s)
    onboarding = OnboardingFlow(bus, interpreter, store)
    links = PanelLinks(settings.panel_origin, redis)
    if settings.attachments_s3_bucket_name:
        attachment_store = S3AttachmentStore(
            endpoint_url=settings.attachments_s3_endpoint_url or "",
            access_key_id=settings.attachments_s3_access_key_id or "",
            secret_access_key=settings.attachments_s3_secret_access_key or "",
            bucket_name=settings.attachments_s3_bucket_name,
            region=settings.attachments_s3_region or "",
            url_style=settings.attachments_s3_url_style or "path",
        )
    else:
        attachment_store = MemoryAttachmentStore()
    attachments = AttachmentService(factory, attachment_store, signed_url_ttl_s=settings.attachment_signed_url_ttl_s)
    pipeline = TurnPipeline(identity=identity, onboarding=onboarding, orchestrator=orchestrator, bus=bus, queries=queries,
                            brokerage_queries=brokerage_queries, store=store,
                            turns=turns, messaging=messaging,
                            transcriber=LlmTranscriber(roles, request_timeout_s=settings.llm_request_timeout_s), alerter=alerter,
                            links=links, attachments=attachments, incidents=incidents)
    return Container(settings, engine, factory, redis, alerter, rates, bus, queries, brokerage_queries, turns, store, identity, messaging, roles,
                     orchestrator, onboarding, attachments, pipeline, links, incidents)
