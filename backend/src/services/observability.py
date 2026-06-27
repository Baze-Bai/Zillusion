"""Observability integration for LangSmith/Langfuse tracing."""

from __future__ import annotations

import logging
import os

from src.config import settings

logger = logging.getLogger(__name__)


def setup_observability() -> None:
    """Configure LangSmith/Langfuse tracing based on settings."""
    if not settings.observability.enabled:
        logger.info("Observability disabled")
        return

    # LangSmith setup
    if settings.observability.langsmith_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.observability.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.observability.langsmith_project
        logger.info("LangSmith tracing enabled (project: %s)", settings.observability.langsmith_project)

    # Langfuse setup (alternative)
    if settings.observability.langfuse_public_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.observability.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.observability.langfuse_secret_key
        os.environ["LANGFUSE_HOST"] = settings.observability.langfuse_host
        logger.info("Langfuse tracing enabled")
