import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv("/var/www/coin2win/.env")
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)


def _month_bounds(period_key: str):
    start = datetime.strptime(period_key, "%Y-%m").replace(tzinfo=timezone.utc)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def run_global_billing(period_key: str):
    results = []
    period_start, period_end = _month_bounds(period_key)

    with engine.begin() as conn:
        edges = conn.execute(text("""
            SELECT parent_id, child_id, billing_cycle
            FROM billing_edges
            WHERE is_active = TRUE
              AND billing_cycle = 'monthly'
            ORDER BY id
        """)).mappings().all()

        for e in edges:
            parent_id = e["parent_id"]
            child_id = e["child_id"]

            subtree_ids = [
                r[0]
                for r in conn.execute(text("""
                    WITH RECURSIVE downline AS (
                        SELECT id
                        FROM users
                        WHERE id = :child_id
                        UNION ALL
                        SELECT u.id
                        FROM users u
                        JOIN downline d ON u.parent_id = d.id
                    )
                    SELECT id
                    FROM downline
                """), {"child_id": child_id}).fetchall()
            ]

            if not subtree_ids:
                subtree_ids = [child_id]

            sportsbook_ggr = 0.0
            casino_ggr = 0.0

            dice_ggr = conn.execute(text("""
                SELECT COALESCE(SUM(amount_usd - payout), 0)
                FROM dice_bets
                WHERE user_id = ANY(:ids)
                  AND created_at >= :period_start
                  AND created_at < :period_end
            """), {
                "ids": subtree_ids,
                "period_start": period_start,
                "period_end": period_end,
            }).scalar() or 0

            crash_ggr_local = conn.execute(text("""
                SELECT COALESCE(SUM(amount_usd - payout), 0)
                FROM crash_bets
                WHERE user_id = ANY(:ids)
                  AND created_at >= :period_start
                  AND created_at < :period_end
            """), {
                "ids": subtree_ids,
                "period_start": period_start,
                "period_end": period_end,
            }).scalar() or 0

            crash_ggr_global = conn.execute(text("""
                SELECT COALESCE(SUM(amount_usd - payout), 0)
                FROM global_crash_bets
                WHERE user_id = ANY(:ids)
                  AND created_at >= :period_start
                  AND created_at < :period_end
            """), {
                "ids": subtree_ids,
                "period_start": period_start,
                "period_end": period_end,
            }).scalar() or 0

            crash_ggr = float(dice_ggr or 0) + float(crash_ggr_local or 0) + float(crash_ggr_global or 0)

            row = conn.execute(text("""
                SELECT * FROM run_billing_edge(
                    :parent_id,
                    :child_id,
                    :period_key,
                    :sportsbook_ggr,
                    :casino_ggr,
                    :crash_ggr,
                    NULL
                )
            """), {
                "parent_id": parent_id,
                "child_id": child_id,
                "period_key": period_key,
                "sportsbook_ggr": sportsbook_ggr,
                "casino_ggr": casino_ggr,
                "crash_ggr": crash_ggr,
            }).fetchone()

            results.append({
                "run_id": row[0],
                "parent_id": row[1],
                "child_id": row[2],
                "billing_mode": row[3],
                "period_key": row[4],
                "player_count": row[5],
                "pph_amount": float(row[6]),
                "ggr_amount": float(row[7]),
                "total_amount": float(row[8]),
                "sportsbook_ggr": float(sportsbook_ggr),
                "casino_ggr": float(casino_ggr),
                "crash_ggr": float(crash_ggr),
            })

    return results
