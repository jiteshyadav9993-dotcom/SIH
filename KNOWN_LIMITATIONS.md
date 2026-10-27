# Known limitations

## Demonstration data, not live railway feeds

The defects and the train timetable are realistic stand-ins, not live feeds from the track, signal, traction, or control-office systems. Connecting those systems later is an integration task. A translation layer already sits between the planner and the screen, so the scheduling logic does not have to be redesigned when real records replace the sample set.

The backlog on the console is a saved demo list, so a restart does not wipe it. The first list is the built-in set of demo defects. Each cycle keeps unfinished defects, makes each one a day older, and adds new defects chosen at random. The open list never grows past 20 defects.

## Simulated clock and calendar

The project runs on one simulated clock and calendar, owned by the server. The planning console does not read the laptop's date or time. The screen shows a day number, a week number, and a weekday, for example "Day 12 - Week 2 - Thu". It does not show a calendar date or a month name.

An internal Monday is used so the existing weekday rules, including the Sunday mega-block, keep working. That calendar date is not shown.

Every day advances, including a day on which no maintenance is completed. The daily record still lists that day, with no blocks and zero totals.

Audit rows, sanctioned possessions, caution orders, backlog updates, and completed-maintenance rows store simulated timestamps. A stored timestamp can be turned back into the day it belongs to.

The clock is written to the database about once a real second while it is running, and also when the speed changes, when the day rolls over, and when the server shuts down. After a restart it continues from the saved day, minute, and speed. Simulated time does not pass while the server is off. A force-kill can lose up to one real second of clock progress.

If no page asks for the clock for 15 seconds, the clock pauses. It does not catch up the missed time when a page connects again. The speed buttons are one, five, and ten times: 30, 6, and 3 real minutes per simulated day.

## Possession windows

The possession windows are:

- Night, 00:00–04:00, on days other than Sunday
- Sunday mega-block, 00:00–07:00, in place of the short night
- Daytime off-peak, 11:30–14:30
- Extended block, 00:00–12:00, used by the multi-day plan when a job will not fit a Sunday mega-block
- Emergency, which can start at the current clock, at any time of the operating day

The console offers up to three different windows for the same bundle. Clearing a possession remembers that window in an in-memory set for this process only. The set is not written to the database, and resetting the demo backlog clears it. After a restart the set is empty, so a window used before the restart can be offered again. While the process is running, the next plan starts at the earliest window that is not already in the past and not in that set.

A planned window is named by its simulated day, for example Day 14 - Sun. The clock keeps moving through later days whether or not a possession is open.

`PLANNING_LEAD_MIN` of 30 minutes is a demo shortcut. A window on the current simulated day is not offered unless it starts at least 30 minutes from the simulated now. Real planned blocks are sanctioned in advance through the weekly and monthly plan. Later days are not held back by that lead time.

Only one block is active at a time. Sanctioning a block reserves its defects as SCHEDULED. They stay on the backlog and drop out of the next plan until the block is withdrawn, rejected, or cleared.

## Site gang reports and overrun

Site-gang reports are simulated by the clock. There is no separate gang console. When the simulated minute reaches each point on the sanctioned block, the clock posts one message onto a board that every role sees: the request, the sanction, the line closing (including the station masters at both ends), the gang arriving, and, on a power block, the traction power controller. Work complete is posted at the planned end plus one stored jitter between −10 and +20 simulated minutes. The same jitter is kept on the possession, so a restart draws it again. A positive jitter is overrun. A negative jitter reports early.

The line stays closed on overrun until the Chief clears it. Overrun minutes are the clearance time minus the planned end, and while the block is still open past that end they are the clock minus the planned end. Clear Line is refused until the site report is on the board. Withdraw before the start reopens the line at once. After mobilising has begun, withdraw moves the block to demobilising and the line stays closed until the gang reports the track restored and the Chief clears. That clearance is a withdrawal: the defects stay open and the window is not marked used. If work had started, a 30 km/h caution is imposed.

## Long jobs and the extended block

Some long jobs do not fit any window in the 14 windows the console searches, and they stay waiting. Deep screening is the clearest case: the work itself is about three hours, and once the ballast machine has travelled to site and back the full closure is about ten hours, longer than a seven-hour Sunday. Some tamping jobs are in the same position when the machine has a long way to travel. Those jobs would need a longer block.

The multi-day plan still handles a job that cannot fit a normal Sunday. It is held for the first Sunday with room, and that day opens as a twelve-hour extended block instead of being dropped. A job longer than twelve hours would still have to wait for a later plan.

## An explainable priority, plus one forecast

How urgently a defect is planned comes from a written formula: how serious it is, how overdue it is, whether a speed restriction is already in force, and how busy the section is. Alongside that, one trained model estimates how many days remain before the defect becomes critical. This split is deliberate. Railway safety scheduling needs a priority a controller can inspect and defend, which a single black-box model would not provide.

That "days to critical" model is trained on synthetic labels, not on real defect history. On data like its training data, R2 is about 0.98. On a deliberately harder shifted test set, R2 is about 0.28. R2 is a fit score where 1 would be a perfect match. If the machine-learning library cannot load, the app falls back to the written degradation rule.

## Caution orders

The Caution button creates and records a temporary speed-restriction order. It does not yet slow the trains drawn on the map.

## Data ingestion

An ingestion layer reads track, signal, traction, and control-office export formats, checks each row, and reports which rows were accepted. Each upload replaces the previous one. A later upload does not merge with defects accepted earlier. If nothing usable has been ingested, the console keeps the saved demo backlog. See `INGESTION.md` for which parts of that pipeline are real integration work and which severity mappings and category groupings are still engineering assumptions.

## No login

This demo build has no login. The console actions are open. The audit record still stamps a fixed role name on each action: maintenance officer or section controller.

## What the busy-traffic tests changed

We deliberately stress-tested the solver on dense-traffic cases, including a possession the timetable left with no safe gap. That diagnosis is kept in `debug_infeasible.py`. Those runs are why a safety-critical defect that cannot be placed with its neighbours is tried again on its own, and why an oversized job on the multi-day plan is offered an extended block rather than left off the plan.

## What survives a restart, and what does not

Sanctioned possessions, dossier submissions, temporary speed-restriction orders, and the simulated clock are written to a database. With no configuration the file is `block_planning.db` (SQLite) in the project directory. When `DATABASE_URL` is set, the same tables are created in Postgres. Each action adds a row. Older rows stay as they were. The clock row is a single saved day, minute, and speed. A restart continues from that row. A force-kill can lose up to one real second of clock progress.

A dossier row records the plan and the maintenance officer. A rejection adds a second row with the section controller and the reason. A possession row records the job, line, section, times, machine, power sector, plan, whether the solver found a window, whether it was an emergency, the section controller, and the possession the console is using. A later reject, clear, or shift adds another row. A caution row records the line, chainage, speed cap, extra running time, and the section controller.

On startup the newest possession row is read. When that block is still open, including one that is demobilising after a withdrawal, it is put back on the console, including its dossier id, and it resumes at the phase the simulated clock has reached. When it was cleared, rejected, or withdrawn, the console starts with no active possession. Past rows can be read back from the history. Block messages are stored in the same database and are not posted a second time after a restart. Only one block is active at a time.

The live screen state is one copy inside a single running process. It is not safe across several server workers: each worker would keep its own copy, and those copies are not kept in step. The database is the shared record.

These live values are still memory-only. A restart does not load them back:

- the officer name on the live console. It remains on the dossier rows.
- the custom priority weights and the earliest start time.
- the live caution order on the screen. The order remains in the database, and it is not applied to the trains on the map.
- the live rejection reason. The reason remains on the dossier and possession rows.
- the windows already used for a cleared possession. That set lives only in the running process. Resetting the demo backlog clears it. It is not stored in the database.
