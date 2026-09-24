# Data ingestion layer (TMS / SMMS / TDMS / COA)

This describes `data_ingestion.py`, the `sample_data/` fixtures, and the
`POST /api/ingest` endpoint. It exists so a judge can see, end to end, what a
real integration with Central Railway's four source systems would look like
— and, just as importantly, exactly which parts of that are real
architecture today versus which parts are still a stand-in for a live feed.

## What this is

Four source-specific parsers (`parse_tms_csv`, `parse_smms_csv`,
`parse_tdms_json`, `parse_coa_json` in `data_ingestion.py`), each reading a
**distinct, realistic export schema**:

| Source | Format | Distinguishing traits |
|---|---|---|
| TMS  (Track Management System) | CSV | numeric mainframe line codes (`LINE_ID` 1-7), `DD-MM-YYYY` dates, a legacy 1-10 defect grade |
| SMMS (Signalling Maintenance & Mgmt System) | CSV | spaced line names (`UP FAST`), overdue days reported directly, text priority bands (`CRITICAL`/`MAJOR`/`MINOR`/`ROUTINE`) |
| TDMS (Traction Distribution Management System) | JSON | ISO-8601 dates, a raw physical measurement (`sag_measurement_mm`) instead of any severity code |
| COA  (Control Office Application) | JSON | sanctioned corridor block/possession windows — not a defect feed at all |

Each parser normalizes its rows onto the existing `defects.TrackDefect`
dataclass by calling `defects._make_defect()` directly, so:

- severity-range validation (`[1, 100]`),
- "does this line physically exist at this chainage" validation, and
- derivation of `section_id`, `power_sector_id`, and `is_mandatory`

are **not duplicated** — they're the same code path `get_default_defects()`
already uses. A bad row (unknown line code, out-of-corridor km, an
out-of-range legacy grade, a missing column, an unrecognised category) is
rejected with a specific per-row reason and does not abort the rest of the
file. `sample_data/*` deliberately includes a handful of bad rows in every
file so this is visible in a live demo, not just asserted in a docstring.

COA records normalize onto a new `CorridorBlockWindow` record (not
`TrackDefect` — a sanctioned window isn't a defect) with the same
reject-with-reason behaviour.

`POST /api/ingest` accepts either real file uploads (`tms_file`,
`smms_file`, `tdms_file`, `coa_file` — any subset) or, for the demo, a
`folder` form field that reads the four well-known filenames straight off
disk (defaulting to `sample_data/`). It returns records-read /
accepted / rejected-with-reasons per source. Accepted defects become the
process-wide "effective" defect set consumed by `/api/master-data`,
`/api/optimize`, and `/api/horizon-plan` (`data_ingestion.get_effective_defects()`).
If nothing has ever been ingested, or every row across every source was
rejected, those endpoints keep calling `defects.get_default_defects()`
automatically — the original synthetic demo still runs standalone with zero
setup.

## What's still simulated, and why

Being direct about this for a judged submission:

- **The fixture files are hand-written, not a live feed.** `sample_data/`
  is a believable stand-in for a TMS/SMMS/TDMS/COA export, generated once,
  not pulled from a real Central Railway system. Swapping in a real export
  means pointing `ingest_from_folder()` (or the upload endpoint) at real
  files with the same column/field names documented above — the parsing,
  validation, and fallback logic do not change.

- **The severity-mapping formulas are illustrative engineering
  assumptions, not RDSO/IR tolerance tables.** Specifically:
  - TMS: `severity = clamp(GRADE * 10, 1, 100)` from a 1-10 legacy grade.
  - TDMS: `severity = clamp(round(sag_mm / 3), 1, 100)` from a raw
    conductor-sag measurement (roughly: ~30 mm sag treated as minor, ~300 mm
    treated as critical). A real integration would use the actual OHE
    contact-wire tension/sag tolerance table for the relevant traction
    zone, not a fixed divisor.
  - SMMS: `PRIORITY_BAND` text is mapped to a fixed severity per band
    (`CRITICAL=95, MAJOR=78, MINOR=55, ROUTINE=35`) rather than a
    continuous score.

- **Several source-specific fault/defect subtypes collapse onto one
  internal `DefectType` each.** SMMS's `INTERLOCK_FAULT`,
  `POINT_MACHINE_FAULT`, `TRACK_CIRCUIT_FAULT`, and `AXLE_COUNTER_FAULT`
  all become `SIGNAL_INTERLOCK`; TDMS's `CONDUCTOR_SAG`,
  `CONTACT_WIRE_WEAR`, and `DROPPER_DAMAGE` all become `OHE_SAG`. This is a
  real limitation of today's `TrackDefect` model (it has one signalling
  bucket and one traction bucket), not of the ingestion layer — extending
  `DefectType` with real subtypes would let these carry through instead of
  merging.

- **COA sanctioned block windows are parsed and validated, but not
  enforced.** They're available via
  `data_ingestion.get_effective_coa_windows()` and returned in the ingest
  summary, but `solver.py` does not currently check a scheduled possession
  against them. Wiring that in — treating a COA window as a hard constraint
  on when a bundle's possession is allowed to start — is a solver-level
  change and was explicitly kept out of scope here per the "don't touch
  `solver.py`" constraint.

- **The reference date (`as_of`) for overdue-day calculations defaults to
  "today."** TMS and TDMS derive `days_overdue` from a due-date compared
  against `date.today()` at ingest time; SMMS reports overdue days
  directly (a real point of heterogeneity between the two kinds of legacy
  system, kept rather than normalized away). A real feed would carry its
  own "as-of" timestamp per export rather than trusting the ingesting
  machine's clock.

- **No persistence, no auth, single-process only.** The "effective defect
  set" lives in an in-memory module-level cache in `data_ingestion.py` for
  the lifetime of the running process; it resets on restart and isn't
  shared across multiple worker processes. `/api/ingest` has no
  authentication. Both are appropriate for a demo server and both are the
  first things a production deployment would need to add.

## Quick way to see it work

```bash
curl -X POST http://127.0.0.1:8000/api/ingest -F folder=sample_data
curl http://127.0.0.1:8000/api/master-data   # now shows the ingested backlog
```

or upload real files instead of using `folder`:

```bash
curl -X POST http://127.0.0.1:8000/api/ingest \
  -F tms_file=@sample_data/tms_defects.csv \
  -F smms_file=@sample_data/smms_defects.csv \
  -F tdms_file=@sample_data/tdms_defects.json \
  -F coa_file=@sample_data/coa_block_windows.json
```
