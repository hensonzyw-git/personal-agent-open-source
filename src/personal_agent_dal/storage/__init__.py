"""DAL workflow service storage.

The DAL keeps its own SQLite database, separate from both the Agent API and
the Finance MCP databases, under its own service directory. The schema itself
arrives with DAL-008 (`DAL-T-DB-CONTRACT-001`); DAL-007 establishes only the
independent database boundary: its own engine, its own Alembic migration
environment, and its own declarative base, so no later schema can be pointed
at another service's database by accident.
"""
