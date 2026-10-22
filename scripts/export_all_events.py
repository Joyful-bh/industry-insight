from __future__ import annotations

# ruff: noqa: E402,I001 -- Prefer the workspace source over the packaged runtime.

import csv
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from track_insight.infrastructure.database import get_engine


QUERY = text(
    """
    SELECT
        id AS event_id,
        page_analysis_id,
        sequence_no,
        event_type,
        event_status,
        title,
        summary,
        signal_date,
        date_precision,
        regions::text AS regions,
        industries::text AS industries,
        industry_objects::text AS industry_objects,
        topic_hint,
        entities::text AS entities,
        chain_roles::text AS chain_roles,
        smb_relevance,
        smb_reason,
        confidence,
        event_fingerprint,
        created_at
    FROM event
    ORDER BY created_at, id
    """
)


def main() -> None:
    output = ROOT / "outputs/all_events.csv"
    with (
        get_engine().connect() as connection,
        output.open("w", encoding="utf-8-sig", newline="") as stream,
    ):
        result = connection.execution_options(stream_results=True).execute(QUERY)
        writer = csv.writer(stream)
        writer.writerow(result.keys())
        count = 0
        for row in result:
            writer.writerow(row)
            count += 1
    print(count)


if __name__ == "__main__":
    main()
