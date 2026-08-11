"""arq task: periodic Gmail sync for orgs with the integration connected.

Cron-scheduled (arq cron_jobs). Feeds new messages to ingestion when the org
opted in to email-as-knowledge.
"""

from __future__ import annotations

# TODO(M12): async def sync_email(ctx, integration_id) — incremental via historyId
