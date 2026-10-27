"""Silver and Gold layers, computed by Spark (over Spark Connect) on Iceberg tables.

Silver: cleaned, de-duplicated, latest-version-wins (MERGE INTO for CDC sources).
Gold:   business-ready aggregates that dashboards, the Data API and the AI agent read through Trino.
"""

from __future__ import annotations

from ..storage import spark, table_exists

ERP_KEYS = {"plot": "plot_id", "tenant": "tenant_id", "employee": "employee_id", "asset": "asset_id",
            "purchase_order": "po_id"}


def _sql(statement: str) -> None:
    spark().sql(statement).collect()


def silver() -> dict[str, int]:
    s = spark()
    for ns in ("silver", "gold"):
        _sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    counts: dict[str, int] = {}

    # ERP: MERGE the newest version of every changed key (CDC from Bronze)
    for table, key in ERP_KEYS.items():
        if not table_exists(f"bronze.erp_{table}"):
            continue
        cols = ", ".join(c for c in s.table(f"bronze.erp_{table}").columns if not c.startswith("_"))
        latest = (f"SELECT {cols} FROM (SELECT *, row_number() OVER (PARTITION BY {key} "
                  f"ORDER BY updated_at DESC, _ingested_at DESC) AS _rn FROM bronze.erp_{table}) WHERE _rn = 1")
        _sql(f"CREATE TABLE IF NOT EXISTS silver.{table} USING iceberg AS {latest} LIMIT 0")
        # only new or newer keys: an empty MERGE would still rewrite files and add a snapshot
        changed = (f"SELECT b.* FROM ({latest}) b LEFT JOIN silver.{table} t ON t.{key} = b.{key} "
                   f"WHERE t.{key} IS NULL OR b.updated_at > t.updated_at")
        if s.sql(f"SELECT count(*) FROM ({changed})").collect()[0][0]:
            _sql(f"""MERGE INTO silver.{table} t USING ({changed}) b ON t.{key} = b.{key}
                     WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")
        counts[table] = s.table(f"silver.{table}").count()

    if table_exists("bronze.weighbridge"):
        _sql("""CREATE OR REPLACE TABLE silver.weighbridge USING iceberg PARTITIONED BY (weighed_date) AS
                SELECT ticket_no, tenant_id, truck_plate, gross_kg, tare_kg, gross_kg - tare_kg AS net_kg,
                       CAST(weighed_at AS TIMESTAMP) AS weighed_at, CAST(CAST(weighed_at AS TIMESTAMP) AS DATE) AS weighed_date,
                       gross_kg > tare_kg AS is_valid
                FROM (SELECT *, row_number() OVER (PARTITION BY ticket_no ORDER BY _ingested_at DESC) AS _rn
                      FROM bronze.weighbridge) WHERE _rn = 1""")
        counts["weighbridge"] = s.table("silver.weighbridge").count()

    if table_exists("bronze.ticket"):
        _sql("""CREATE OR REPLACE TABLE silver.ticket USING iceberg AS
                SELECT ticket_id, tenant_id, category, priority, status, description,
                       CAST(opened_at AS TIMESTAMP) AS opened_at, CAST(closed_at AS TIMESTAMP) AS closed_at,
                       ROUND((unix_timestamp(CAST(closed_at AS TIMESTAMP)) - unix_timestamp(CAST(opened_at AS TIMESTAMP))) / 3600.0, 1)
                         AS resolution_hours
                FROM (SELECT *, row_number() OVER (PARTITION BY ticket_id ORDER BY _ingested_at DESC) AS _rn
                      FROM bronze.ticket) WHERE _rn = 1""")
        counts["ticket"] = s.table("silver.ticket").count()

    if table_exists("bronze.ebus_telemetry"):
        _sql("""CREATE OR REPLACE TABLE silver.ebus_telemetry USING iceberg PARTITIONED BY (days(event_time)) AS
                SELECT bus_id, route_id, event_time, lat, lon, speed_kmh, battery_pct, passengers
                FROM (SELECT *, row_number() OVER (PARTITION BY bus_id, event_time ORDER BY event_time) AS _rn
                      FROM bronze.ebus_telemetry
                      WHERE lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 AND battery_pct BETWEEN 0 AND 100)
                WHERE _rn = 1""")
        counts["ebus_telemetry"] = s.table("silver.ebus_telemetry").count()
    return counts


GOLD = {
    "weighbridge_daily": """
        SELECT tenant_id, weighed_date AS day, count(*) AS trucks, ROUND(sum(net_kg) / 1000.0, 2) AS net_tons
        FROM silver.weighbridge WHERE is_valid GROUP BY tenant_id, weighed_date""",
    "ticket_sla": """
        SELECT tenant_id, category, count(*) AS tickets, sum(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_tickets,
               ROUND(avg(resolution_hours), 1) AS avg_resolution_hours,
               sum(CASE WHEN resolution_hours > 48 OR (status = 'open' AND opened_at < current_timestamp() - INTERVAL 48 HOURS)
                        THEN 1 ELSE 0 END) AS sla_breaches
        FROM silver.ticket GROUP BY tenant_id, category""",
    "procurement_monthly": """
        SELECT department, date_trunc('MONTH', ordered_at) AS month, count(*) AS po_count,
               CAST(sum(amount) AS DECIMAL(18,2)) AS total_amount_idr
        FROM silver.purchase_order GROUP BY department, date_trunc('MONTH', ordered_at)""",
    "headcount": """
        SELECT department, count(*) AS headcount, CAST(sum(salary) AS DECIMAL(18,2)) AS total_salary_idr
        FROM silver.employee GROUP BY department""",
    "contract_expiry": """
        SELECT f.tenant_id, f.doc_id, f.file_name, f.lease_start, f.lease_end, f.monthly_fee_idr,
               datediff(f.lease_end, current_date()) AS days_to_expiry, f.needs_review, f.model, f.extracted_at
        FROM (SELECT x.*, row_number() OVER (PARTITION BY x.tenant_id
                                             ORDER BY d._ingested_at DESC, x.extracted_at DESC) AS _rn
              FROM silver.document_fields x JOIN bronze.document d ON d.doc_id = x.doc_id
              WHERE x.doc_type = 'lease_agreement') f
        WHERE f._rn = 1  -- newest document version, then its newest extraction""",
    "tenant_overview": """
        SELECT t.tenant_id, t.name, t.sector, t.plot_id, p.zone, p.area_m2, t.status,
               c.lease_start, c.lease_end, c.monthly_fee_idr,
               coalesce(k.open_tickets, 0) AS open_tickets, coalesce(w.net_tons_30d, 0) AS net_tons_30d
        FROM silver.tenant t
        LEFT JOIN silver.plot p ON p.plot_id = t.plot_id
        LEFT JOIN gold.contract_expiry c ON c.tenant_id = t.tenant_id
        LEFT JOIN (SELECT tenant_id, sum(open_tickets) AS open_tickets FROM gold.ticket_sla GROUP BY tenant_id) k
               ON k.tenant_id = t.tenant_id
        LEFT JOIN (SELECT tenant_id, sum(net_tons) AS net_tons_30d FROM gold.weighbridge_daily
                   WHERE day >= date_sub(current_date(), 30) GROUP BY tenant_id) w ON w.tenant_id = t.tenant_id""",
    "fleet_daily": """
        SELECT bus_id, CAST(event_time AS DATE) AS day, count(*) AS readings, ROUND(avg(speed_kmh), 1) AS avg_speed_kmh,
               min(battery_pct) AS min_battery_pct, max(passengers) AS max_passengers
        FROM silver.ebus_telemetry GROUP BY bus_id, CAST(event_time AS DATE)""",
    "bus_latest_position": """
        SELECT bus_id, route_id, event_time, lat, lon, speed_kmh, battery_pct, passengers
        FROM (SELECT *, row_number() OVER (PARTITION BY bus_id ORDER BY event_time DESC) AS _rn
              FROM silver.ebus_telemetry) WHERE _rn = 1""",
}
GOLD_NEEDS = {"weighbridge_daily": "silver.weighbridge", "ticket_sla": "silver.ticket",
              "procurement_monthly": "silver.purchase_order", "headcount": "silver.employee",
              "contract_expiry": "silver.document_fields", "tenant_overview": "silver.tenant",
              "fleet_daily": "silver.ebus_telemetry", "bus_latest_position": "silver.ebus_telemetry"}


def gold() -> dict[str, int]:
    s = spark()
    _sql("CREATE NAMESPACE IF NOT EXISTS gold")
    counts = {}
    for name, query in GOLD.items():  # dict order = dependency order
        if not table_exists(GOLD_NEEDS[name]):
            continue
        if name == "tenant_overview" and not table_exists("gold.contract_expiry"):
            continue
        _sql(f"CREATE OR REPLACE TABLE gold.{name} USING iceberg AS {query}")
        counts[name] = s.table(f"gold.{name}").count()

    # Share: data products for parties outside the estate. A fixed, documented shape that does not change when
    # internal tables do; who sees which rows is decided per identity in Trino (see docs/production.md).
    _sql("CREATE NAMESPACE IF NOT EXISTS share")
    for name, query in SHARE.items():
        if table_exists(SHARE_NEEDS[name]):
            _sql(f"CREATE OR REPLACE TABLE share.{name} USING iceberg AS {query}")
            counts[f"share.{name}"] = s.table(f"share.{name}").count()
    return counts


SHARE = {"tonnage_daily": "SELECT tenant_id, day, trucks, net_tons FROM gold.weighbridge_daily"}
SHARE_NEEDS = {"tonnage_daily": "gold.weighbridge_daily"}


def maintenance(retain_days: int = 7) -> dict[str, int]:
    """Compact small files and expire old snapshots (time travel window = retain_days)."""
    s = spark()
    done = 0
    for ns in ("bronze", "silver", "gold", "share"):
        for row in s.sql(f"SHOW TABLES IN {ns}").collect():
            name = f"lakehouse.{ns}.{row.tableName}"
            _sql(f"CALL lakehouse.system.rewrite_data_files(table => '{name}')")
            _sql(f"CALL lakehouse.system.expire_snapshots(table => '{name}', "
                 f"older_than => current_timestamp() - INTERVAL {retain_days} DAYS, retain_last => 5)")
            done += 1
    return {"tables_maintained": done}
