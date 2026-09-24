from __future__ import annotations

import datetime
import sys

sys.path.insert(0, r"C:\Users\JITESH\Downloads\PythonProject5")

import defects
import calendar_plan

bundles = defects.get_active_maintenance_bundles()
plan = calendar_plan.build_horizon_plan(list(bundles), datetime.date(2026, 9, 24), 7)
total_assigned = 0
for day in plan.days:
    n = len(day.assigned_bundle_ids)
    total_assigned += n
    status = day.result.status if day.result else "NO_WORK"
    weekday = day.calendar_date.strftime("%A")
    print(
        f"{day.calendar_date} {weekday:9s} window={day.window.label:12s} "
        f"bundles={n} status={status} trains_in_play={day.trains_in_play} "
        f"assigned={day.assigned_bundle_ids}"
    )
print("unscheduled:", plan.unscheduled_bundle_ids)
print("total bundles going in:", len(bundles), " total assigned across week:", total_assigned)
