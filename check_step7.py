import json
import os

if os.path.exists("test_step7.db"):
    os.remove("test_step7.db")

os.environ["DATABASE_URL"] = "sqlite:///./test_step7.db"

from fastapi.testclient import TestClient
import app

c = TestClient(app.app)

c.get("/api/master-data")

print(c.get("/api/maintenance-log").json()["summary"])

for _ in range(7):
    c.post("/api/officer/submit")
    sanction = c.post("/api/chief/sanction").json()
    c.post("/api/chief/clear")
    if sanction["status"] != "SANCTIONED":
        break

print(json.dumps(c.get("/api/maintenance-log").json()["summary"], indent=2))
