"""
SQLAlchemy declarative Base + shared schema conventions.

Every model in app/postgres/models/ inherits from `Base`. Naming
convention is pinned so constraint names are deterministic across
environments regardless of how the driver auto-names them.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


NAMING_CONVENTION = {
    'ix':  'ix_%(column_0_label)s',
    'uq':  'uq_%(table_name)s_%(column_0_name)s',
    'ck':  'ck_%(table_name)s_%(constraint_name)s',
    'fk':  'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk':  'pk_%(table_name)s',
}


metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base. All Postgres models
    inherit from this so `Base.metadata.create_all()` sees every table."""
    metadata = metadata
