# Known limitations

## Long jobs and the extended block

Some heavy jobs need the line closed for longer than a normal night (about four hours) or a standard Sunday block (about seven hours). Deep screening is the clearest case in this demo: the work itself is three hours, and once the ballast machine has travelled to site and back the full closure is about ten hours. The planner already handles this. A job that cannot fit a Sunday is held for the first Sunday with room, and that day opens as a twelve-hour extended block instead of being dropped. A job longer than twelve hours would still have to wait for a later plan.

## Demonstration data, not live railway feeds

The defects and the timetable in this demo are realistic stand-ins, not live feeds from the track, signal, traction, and control-office systems (TMS, SMMS, TDMS, and COA). Connecting those systems later is an integration task. A translation layer already sits between the planner and the screen, so the scheduling logic does not have to be redesigned when real records replace the sample set.

An actual ingestion layer for this now exists (`data_ingestion.py`, sample exports under `sample_data/`, `POST /api/ingest`): it parses source-specific TMS/SMMS/TDMS/COA formats, normalizes them onto `defects.TrackDefect` with full validation, and reports per-row acceptance/rejection. It falls back to the synthetic backlog below automatically if nothing has been ingested. See `INGESTION.md` for exactly which parts of that pipeline are real integration architecture versus which severity-mapping formulas and category groupings are still placeholder engineering assumptions.

## An explainable priority, plus one forecast

How urgently a defect is planned comes from a written formula: how serious it is, how overdue it is, whether a speed restriction is already in force, and how busy the section is. Alongside that, one trained model estimates how many days remain before the defect becomes critical. This split is deliberate. Railway safety scheduling needs a priority a controller can inspect and defend, which a single black-box model would not provide.

## What the busy-traffic tests changed

We deliberately stress-tested the solver on dense-traffic cases, including a possession the timetable left with no safe gap. That diagnosis is kept in `debug_infeasible.py`. Those runs are why a safety-critical defect that cannot be placed with its neighbours is tried again on its own, and why an oversized job is offered an extended block rather than left off the plan.

## What survives a restart, and what does not

Sanctioned possessions, dossier submissions, and temporary speed-restriction orders are written to a database by `persistence.py`. With no configuration the file is `block_planning.db` (SQLite) in the project directory. When `DATABASE_URL` is set, the same tables are created in Postgres. Each action adds a row. Older rows stay as they were.

A dossier row records the plan and `submitted_by` as `MAINTENANCE_OFFICER`. A rejection adds a second row with status `REJECTED`, `SECTION_CONTROLLER`, and the reason. A possession row records the bundle, line, section, minutes, machine, power sector, plan, solver status, whether it was an emergency, and `sanctioned_by` as `SECTION_CONTROLLER`, plus the full possession object the console uses. A later reject, clear, or shift adds another row (`REJECTED`, `CLEARED`, or `SHIFTED`). A caution row records the line, chainage, speed cap, extra running time, and `issued_by` as `SECTION_CONTROLLER`.

On startup the process reads the newest possession row. When that row is still open (`SANCTIONED` or `SHIFTED`), it is copied back into `SYSTEM_STATE`. When the newest row is `CLEARED` or `REJECTED`, the console starts with no active possession. The tables can be read at `GET /api/history/possessions` and `GET /api/history/dossiers`.

`SYSTEM_STATE` is still one dictionary inside a single process. It is the copy the screen reads. It is not safe across multiple uvicorn workers: each worker has its own dictionary, and those copies are not kept in step. The database is the shared record.

These live values are still memory-only. A restart does not load them back:

- `submitted_by` and `coa_dossier_id`. The officer console name remains on the dossier rows. After a restart the live copies are empty, including when an open possession has been restored.
- `custom_weights` and `earliest_start_min`.
- the live `tsr` object. The order remains in `tsr_orders`. The console copy is not loaded again.
- the live `reject_reason`. The reason remains on the dossier and possession rows.
