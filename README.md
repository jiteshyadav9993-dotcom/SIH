> ⚠️ **Proprietary Software:** This project is not open-source. See the `LICENSE` file for strict usage and sharing restrictions.

# Automatic Block Planning DSS

**SIH 2026 · Problem Statement 26027 · Ministry of Railways · Team Bug_Hunters**

AI-powered automatic block planning that turns three separate maintenance backlogs into one coordinated, conflict-free possession a controller can sanction.

The problem statement asks for integration, AI/ML prioritisation, multi-department optimisation, and weekly plus monthly plans. This prototype delivers all four on a working CSMT–Pune corridor desk.

## Why this solves 26027

Today Engineering, Traction Distribution, and Signal & Telecommunication each keep defects in TMS, SMMS, and TDMS, each request blocks through BDMS, and COA holds corridor availability. Uncoordinated requests waste possessions and cut asset availability.

This DSS closes that loop:

1. **Ingest** track, signalling, traction, and control-office records into one backlog.
2. **Prioritise** work by criticality, overdue urgency, TSR impact, and section traffic.
3. **Bundle** nearby jobs from different departments into a single possession.
4. **Optimise** the window against the working timetable and a goods-train forecast.
5. **Publish** weekly and monthly block plans for officer and controller action.

A person still sanctions, shifts, or rejects. The system never forces an unsafe window.

## Solution mapped to the official brief

| Expected solution (PS 26027) | What this system delivers |
|---|---|
| Integrate TMS, SMMS, TDMS with corridor availability, the train timetable, and the goods forecast from Control Office | Dedicated parsers for TMS CSV, SMMS CSV, TDMS JSON, and COA JSON. Encoded Central Railway CSMT–Pune corridor (~192 km). Model working timetable of ~170 services. Goods-train forecast used in the solver. BDMS-style report portal feeds the live backlog. |
| AI/ML to prioritise and schedule by criticality, urgency, and asset-availability impact | Formula 1: inspectable urgency (severity, overdue days, TSR, traffic). Gradient Boosting forecast of days until a defect becomes critical. Formula 2: spatial bundling on the same line and power feeder. Google OR-Tools CP-SAT places the possession or reports it infeasible. |
| Optimise blocks to maximise uptime and coordinate multi-department work | One possession for clustered Engineering / Traction / S&T jobs instead of three separate closures. Hard constraints: train vs gang non-overlap, Premium/Superfast protection, delay caps, power-sector isolation, division machine fleet. Punctuality-First, Asset-First, and Mega-Block policies. |
| Weekly and monthly horizons | 7-day and 28-day horizon plans. Night, daytime off-peak, Sunday mega-block, extended block, and emergency windows. Officer dossier, controller sanction / shift / reject, audit trail, and completed-work record. |

## What judges will see in the demo

- CRIS-style planning console with GIS map and track schematic
- Report intake → reviewed findings → ranked backlog → bundled possession
- CP-SAT result the controller can defend: feasible window or a clear infeasible reason
- Weekly / monthly calendar of planned blocks
- Simulated operating clock so a full possession cycle (request, sanction, gang on site, clear, overrun) can be shown in minutes

## Architecture in brief

| Layer | Role |
|---|---|
| `data_ingestion.py` | TMS / SMMS / TDMS / COA export readers and validation |
| Report portal + `report_nlp.py` | Evidence-backed reading of maintenance reports into structured findings |
| `defects.py` | Formula 1 urgency and Formula 2 spatial bundling |
| `ml_priority.py` | Days-to-critical forecast (trained model shipped with the repo) |
| `solver.py` | OR-Tools CP-SAT possession search |
| `calendar_plan.py` | Weekly and monthly horizon assembly |
| `timetable.py` / `metrics.py` | Corridor trajectories, freight forecast, delay impact |
| `app.py` + `index.html` | FastAPI DSS and controller / officer consoles |

The planner is already separated from the source systems: swap sample exports for authorised railway files of the same shape without rewriting the optimiser.

## How to run

From the project folder:

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000. The map tiles need internet; the track schematic runs offline.

```
python -m pytest -q
```

The console backlog starts empty. Apply accepted reports in the BDMS Report Portal to fill it.

## Further reading

- [INGESTION.md](INGESTION.md) — source formats and validation
- [ML_MODEL.md](ML_MODEL.md) — forecast model and evaluation
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) — demo clock, persistence, and integration notes for evaluators
