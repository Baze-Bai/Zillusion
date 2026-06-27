"""SQLAlchemy declarative base for the discovery agent persistence layer."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base — all ORM models inherit from this."""

    pass
