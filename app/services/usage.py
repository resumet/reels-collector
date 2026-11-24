"""NFR-08: 모든 제공사 호출을 api_usage 에 기록."""
from __future__ import annotations

from sqlalchemy import update

from ..db import session_scope
from ..models import ApiUsage, CollectionRun
from ..providers.base import current_run_id


def record_usage(provider: str, endpoint: str, http_status: int | None, units: float, cost: float) -> None:
    run_id = current_run_id.get()
    with session_scope() as s:
        s.add(ApiUsage(run_id=run_id, provider=provider, endpoint=endpoint, http_status=http_status,
                       billed_units=units, cost_usd=cost))
        if run_id and cost:
            s.execute(update(CollectionRun).where(CollectionRun.id == run_id)
                      .values(cost_usd=CollectionRun.cost_usd + cost))
