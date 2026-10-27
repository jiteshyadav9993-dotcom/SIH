import os

if os.path.exists("test_cycle.db"):
    os.remove("test_cycle.db")

os.environ["DATABASE_URL"] = "sqlite:///./test_cycle.db"

from fastapi.testclient import TestClient

import app
import backlog_state

client = TestClient(app.app)

master = client.get("/api/master-data").json()
print(len(master["defects"]))

for _ in range(2):
    client.post("/api/officer/submit")
    client.post("/api/chief/sanction")
    client.post("/api/chief/clear")

master = client.get("/api/master-data").json()
print(len(master["defects"]))

first_open = backlog_state.get_active_defects()[0]
tracked_id = first_open.defect_id
tracked_overdue = first_open.days_overdue

cycle_response = client.post("/api/backlog/next-cycle")
print(cycle_response.text)

master = client.get("/api/master-data").json()
same_defect = next(
    defect for defect in backlog_state.get_active_defects() if defect.defect_id == tracked_id
)
print(len(master["defects"]), same_defect.days_overdue)
if same_defect.days_overdue != tracked_overdue + 1.0:
    print(tracked_id, "days_overdue", tracked_overdue, "->", same_defect.days_overdue)

for _ in range(4):
    cycle = client.post("/api/backlog/next-cycle").json()
    print(cycle["carried_over"], cycle["new_defects"], cycle["open_total"])

open_ids = [defect.defect_id for defect in backlog_state.get_active_defects()]
print(len(open_ids) != len(set(open_ids)))
