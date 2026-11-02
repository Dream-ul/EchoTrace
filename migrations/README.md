# EchoTrace database migrations

The canonical physical contract is `docs/echotrace-rebuild/08-存储与Redis运行契约.md`.
`echotrace.infrastructure.persistence.mysql.models.Base.metadata` contains all
22 V1 tables and is suitable for Alembic autogeneration.  The project keeps
schema creation explicit in the application composition root; importing the
models never opens a connection or mutates a database.

When Alembic is installed in a deployment, apply the checked-in revision with:

```text
python -m alembic -c alembic.ini upgrade head
```

Set `ECHOTRACE_DATABASE_URL` (for example,
`mysql+pymysql://user:password@host:3306/echotrace?charset=utf8mb4`) in the
deployment environment; credentials are intentionally not stored in this
repository.

The migration must preserve the circular `investigation_runs.plan_id` foreign
key ordering described in the storage contract.  The migration contract tests
use an in-memory SQLite engine to verify metadata, constraints, transaction
rollback, idempotent outbox insertion and optimistic state-version updates;
MySQL 8 is the production dialect and must be exercised by the integration
profile when credentials are available.
