import os

from alembic import context
from sqlalchemy import engine_from_config, pool, text

config = context.config
target_metadata = None

def _required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required migration setting: {name}")
    return value

def _migration_target():
    target = _required_env("COIN2WIN_MIGRATION_TARGET").lower()
    if target not in {"local", "test", "staging", "production"}:
        raise RuntimeError("Invalid COIN2WIN_MIGRATION_TARGET")
    if target == "production" and os.environ.get("COIN2WIN_MIGRATION_PRODUCTION_ACK") != "I_UNDERSTAND_THIS_IS_PRODUCTION":
        raise RuntimeError("Production migration target requires explicit acknowledgement")
    return target

def _connection_settings():
    target = _migration_target()
    url = _required_env("COIN2WIN_MIGRATION_DATABASE_URL")
    expected_db = _required_env("COIN2WIN_MIGRATION_EXPECTED_DATABASE")
    return target, url, expected_db

def _verify_database_identity(connection, expected_db):
    actual_db = connection.execute(text("SELECT current_database()")).scalar_one()
    if actual_db != expected_db:
        raise RuntimeError(f"Database identity mismatch: expected {expected_db!r}, got {actual_db!r}")

def run_migrations_offline():
    raise RuntimeError(
        "Offline migration mode is disabled because database identity cannot be verified"
    )

def run_migrations_online():
    _, url, expected_db = _connection_settings()
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _verify_database_identity(connection, expected_db)
        connection.rollback()  # end read-only identity-check transaction before Alembic transaction
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
