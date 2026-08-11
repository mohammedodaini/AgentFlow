# Alembic — created properly at M2

Do not hand-write env.py now. At M2 we run `alembic init -t async alembic`
together and wire it to app.models (import all models so autogenerate sees
them) and settings.database_url. Until then this folder only reserves the
structure. Migrations land in versions/.
