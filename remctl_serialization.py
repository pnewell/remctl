"""Shared reminder serialization helpers for RemCTL."""

from __future__ import annotations

import json


RECURRENCE_FREQUENCIES = {
    0: "daily",
    1: "weekly",
    2: "monthly",
    3: "yearly",
}

DUE_DATE_DELTA_UNITS = {
    0: ("minute", "minutes"),
    1: ("hour", "hours"),
    2: ("day", "days"),
    3: ("week", "weeks"),
    4: ("month", "months"),
}


def _row_has(row, key):
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return key in keys()
        except Exception:
            return False
    return isinstance(row, dict) and key in row


def _row_get(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    if _row_has(row, key):
        return row[key]
    return default


def _json_blob(value):
    if value in (None, ""):
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def recurrence_from_row(row, *, ts=None):
    """Extract a stable recurrence object from aliased ZREMCDOBJECT fields."""
    frequency_raw = _row_get(row, "recurrence_frequency")
    if frequency_raw is None:
        return None
    try:
        frequency = RECURRENCE_FREQUENCIES[int(frequency_raw)]
    except (KeyError, TypeError, ValueError):
        return None

    interval = _row_get(row, "recurrence_interval") or 1
    recurrence = {"frequency": frequency, "interval": int(interval)}

    days_of_week = _json_blob(_row_get(row, "recurrence_days_of_week")) or []
    if days_of_week:
        recurrence["daysOfWeekDetailed"] = days_of_week
        recurrence["daysOfWeek"] = [
            int(item["dayOfTheWeek"])
            for item in days_of_week
            if isinstance(item, dict) and item.get("dayOfTheWeek")
        ]

    for alias, output_key in (
        ("recurrence_days_of_month", "daysOfMonth"),
        ("recurrence_months_of_year", "monthsOfYear"),
        ("recurrence_days_of_year", "daysOfYear"),
        ("recurrence_weeks_of_year", "weeksOfYear"),
        ("recurrence_set_positions", "setPositions"),
    ):
        value = _json_blob(_row_get(row, alias))
        if value:
            recurrence[output_key] = value

    count = _row_get(row, "recurrence_count") or 0
    if count:
        recurrence["count"] = int(count)

    end_date = _row_get(row, "recurrence_end_date")
    if end_date and ts is not None:
        recurrence["endDate"] = ts(end_date).isoformat()

    return recurrence


def due_date_delta_alerts_from_row(row, *, ts=None):
    """Extract Early Reminder due-date delta alerts from a reminder row."""
    payload = _json_blob(_row_get(row, "ZDUEDATEDELTAALERTSDATA"))
    if not isinstance(payload, dict):
        return []
    alerts = payload.get("dueDateDeltaAlerts")
    if not isinstance(alerts, list):
        return []

    result = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        try:
            unit_raw = int(alert.get("dueDateDeltaUnit"))
            count = int(alert.get("dueDateDeltaCount"))
        except (TypeError, ValueError):
            continue
        singular, plural = DUE_DATE_DELTA_UNITS.get(unit_raw, ("unknown", "unknown"))
        value = abs(count)
        unit_name = singular if value == 1 else plural
        direction = "before" if count < 0 else "after"
        item = {
            "unit": unit_name,
            "unitCode": unit_raw,
            "count": count,
            "value": value,
            "direction": direction,
            "label": f"{value} {unit_name} {direction}",
        }
        identifier = alert.get("identifier")
        if identifier:
            item["identifier"] = identifier
        creation_date = alert.get("creationDate")
        if creation_date and ts is not None:
            try:
                item["creationDate"] = ts(float(creation_date)).isoformat()
            except (TypeError, ValueError):
                pass
        min_version = alert.get("minimumSupportedAppVersion")
        if min_version is not None:
            item["minimumSupportedAppVersion"] = min_version
        result.append(item)
    return result


def preload_extras(db, pks):
    """Batch-load subtask counts and hashtags to avoid N+1 queries."""
    if not pks:
        return {}, {}
    placeholders = ",".join("?" * len(pks))
    subtask_rows = db.execute(
        f"SELECT ZPARENTREMINDER, COUNT(*) FROM ZREMCDREMINDER "
        f"WHERE ZPARENTREMINDER IN ({placeholders}) AND ZMARKEDFORDELETION = 0 "
        f"AND ZCOMPLETED = 0 GROUP BY ZPARENTREMINDER",
        pks,
    ).fetchall()
    subtask_counts = {row[0]: row[1] for row in subtask_rows}
    hashtag_rows = db.execute(
        f"SELECT o.ZREMINDER3, h.ZNAME FROM ZREMCDOBJECT o "
        f"JOIN ZREMCDHASHTAGLABEL h ON o.ZHASHTAGLABEL = h.Z_PK "
        f"WHERE o.ZREMINDER3 IN ({placeholders})",
        pks,
    ).fetchall()
    hashtags = {}
    for row in hashtag_rows:
        hashtags.setdefault(row[0], []).append(row[1])
    return subtask_counts, hashtags


def serialize_reminder(
    row,
    *,
    ts,
    priority_names,
    db=None,
    section=None,
    subtask_counts=None,
    hashtags=None,
    rich_link_resolver=None,
    fallback_subtask_count=None,
    fallback_hashtags=None,
    contact_handles_fn=None,
):
    """Convert a reminder row to a JSON-serializable dict."""
    subtask_counts = subtask_counts or {}
    hashtags = hashtags or {}

    subtask_count = subtask_counts.get(row["Z_PK"])
    if subtask_count is None:
        if db is not None and fallback_subtask_count is not None:
            subtask_count = fallback_subtask_count(db, row["Z_PK"])
        else:
            subtask_count = 0

    tags = hashtags.get(row["Z_PK"])
    if tags is None:
        if db is not None and fallback_hashtags is not None:
            tags = fallback_hashtags(db, row["Z_PK"])
        else:
            tags = []

    reminder = {
        "id": row["Z_PK"],
        "title": row["ZTITLE"],
        "list": row["list_name"],
        "completed": bool(row["ZCOMPLETED"]),
        "flagged": bool(row["ZFLAGGED"]),
        "urgent": bool(_row_get(row, "ZISURGENTSTATEENABLEDFORCURRENTUSER", False)),
        "priority": priority_names.get(row["ZPRIORITY"] or 0, "none"),
        "subtaskCount": subtask_count,
        "isSubtask": bool(row["ZPARENTREMINDER"]),
    }
    if section:
        reminder["section"] = section
    if row["ZNOTES"]:
        reminder["notes"] = row["ZNOTES"]

    url = row["ZICSURL"]
    if not url and db is not None and rich_link_resolver is not None:
        url = rich_link_resolver(db, row["Z_PK"])
    if url:
        reminder["url"] = url

    if row["ZDUEDATE"]:
        reminder["dueDate"] = ts(row["ZDUEDATE"]).isoformat()
    display_date = _row_get(row, "ZDISPLAYDATEDATE")
    if display_date and display_date != row["ZDUEDATE"]:
        reminder["displayDate"] = ts(display_date).isoformat()
    if _row_get(row, "ZALLDAY") is not None:
        reminder["allDay"] = bool(_row_get(row, "ZALLDAY"))
    if row["ZCREATIONDATE"]:
        reminder["createdDate"] = ts(row["ZCREATIONDATE"]).isoformat()
    if row["ZCOMPLETIONDATE"]:
        reminder["completionDate"] = ts(row["ZCOMPLETIONDATE"]).isoformat()
    if row["ZPARENTREMINDER"]:
        reminder["parentID"] = row["ZPARENTREMINDER"]
    if tags:
        reminder["tags"] = tags
    recurrence = recurrence_from_row(row, ts=ts)
    if recurrence:
        reminder["recurrence"] = recurrence
    early_reminders = due_date_delta_alerts_from_row(row, ts=ts)
    if early_reminders:
        reminder["earlyReminder"] = early_reminders[0]
        reminder["earlyReminders"] = early_reminders
    contact_handles = contact_handles_fn(row) if contact_handles_fn is not None else None
    if contact_handles:
        reminder["contactHandles"] = contact_handles
    if row["ZCKIDENTIFIER"]:
        reminder["deepLink"] = f"x-apple-reminderkit://REMCDReminder/{row['ZCKIDENTIFIER']}"
    return reminder


def serialize_reminders(
    rows,
    *,
    ts,
    priority_names,
    db=None,
    memberships=None,
    rich_link_resolver=None,
    fallback_subtask_count=None,
    fallback_hashtags=None,
    contact_handles_fn=None,
):
    """Convert reminder rows with shared preloaded metadata."""
    subtask_counts, hashtags = preload_extras(db, [row["Z_PK"] for row in rows]) if db else ({}, {})
    memberships = memberships or {}
    return [
        serialize_reminder(
            row,
            ts=ts,
            priority_names=priority_names,
            db=db,
            section=memberships.get(row["ZCKIDENTIFIER"]),
            subtask_counts=subtask_counts,
            hashtags=hashtags,
            rich_link_resolver=rich_link_resolver,
            fallback_subtask_count=fallback_subtask_count,
            fallback_hashtags=fallback_hashtags,
            contact_handles_fn=contact_handles_fn,
        )
        for row in rows
    ]
