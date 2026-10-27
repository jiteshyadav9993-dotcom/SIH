# Automatic Block Planning

AI-powered automatic block planning for Indian Railways (SIH 2026, problem statement 26027, team Bug_Hunters).

This is a working prototype of a coordinated block-planning desk: three department defect lists, one corridor, one possession a person still has to sanction. It is not a live CRIS integration.

## What it does

- Ranks defects from Engineering, Signalling, and Traction (Formula 1).
- Bundles nearby work on the same running line and feeder (Formula 2).
- Finds a conflict-free possession with OR-Tools CP-SAT, or reports the window infeasible.
- Plans weekly (7-day) and monthly (28-day) windows: night, daytime off-peak, Sunday mega-block, extended block, emergency.
- Takes maintenance reports through a BDMS-style portal, keeps an audit log, and stores completed work in a database.

## What we claim, and what we do not

Mapped to the four expected-solution bullets in SIH-26027 (Ministry of Railways).

| Official ask | What this prototype does | What it does not claim |
|---|---|---|
| Integrate TMS, SMMS, TDMS with COA corridor availability, the working timetable, and a goods forecast | Parsers for those four export shapes, an encoded CSMT–Pune corridor (~192 km, ~170 modelled services), and a seeded freight-forecast jitter on the goods WTT | Live TMS / SMMS / TDMS / COA / BDMS endpoints. Sample files are stand-ins. Severity mappings are engineering assumptions, not RDSO tables. |
| AI/ML to prioritise and schedule by criticality, urgency, and asset impact | An inspectable Formula 1 urgency score. A separate Gradient Boosting forecast of days until severity 80. CP-SAT chooses the window | That the trained model owns safety scheduling. Formula 1 does. ML labels are synthetic, not real defect histories. The report reader is local sentence rules, not a generative model. |
| Optimise blocks, cut downtime, coordinate Engineering, Traction, and S&T | Spatial bundling into one possession; hard rules for trains vs gangs, power isolation, and a model machine fleet; Punctuality-First / Asset-First / Mega-Block policies | A field measurement of train delay saved. Fleet caps are prototype settings. Only one block is active. A caution order is recorded; it does not yet slow trains on the map. |
| Weekly and monthly horizons | `/api/horizon-plan` for 7 and 28 days, with the window types above | A true long-term resource plan. The 28-day view is the daily planner repeated. The console clock is simulated (day / week / weekday), not a calendar month. |

There is no login. Console actions are open; the audit stamp still records a role name.

Full limits: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md), [INGESTION.md](INGESTION.md), [ML_MODEL.md](ML_MODEL.md).

## How to run

From the project folder:

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

`python app.py` starts the server at http://127.0.0.1:8000. Open that address in the browser.

The map background uses OpenStreetMap and OpenRailwayMap, so it needs internet. The track schematic runs offline.

## How to run the tests

```
python -m pytest -q
```

## How the backlog starts

The active backlog starts empty. Accepted maintenance reports in the BDMS Report Portal are what fill it. The synthetic catalog is used by tests and horizon demos; it is not loaded by the console.
