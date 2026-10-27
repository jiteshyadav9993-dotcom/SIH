# Presenter notes

Spoken notes for the six-slide Avishkar deck. Aim for about six and a half minutes. Under the University of Mumbai 2025–26 rules, and under the state guidelines, a podium talk is a maximum of 7 minutes, with a warning bell in the 6th minute and a 15-second grace. Those rules are not confirmed as the rules of this entry. See `AVISHKAR_RULES_CHECK.md`.

Do not say a name, college, department, university, or team. If a slot or code number has been issued, introduce yourself with that number only.

## Slide 1 — title (about 20 seconds)

Read the title once, slowly: integrated railway maintenance prioritisation and constraint-based block planning for asset availability.

Say that the level and the slot or code number are the ones issued at registration, and that you will use that number rather than a name. Then move on. Do not add a biography.

## Slide 2 — problem and objective (about 70 seconds)

The problem is coordination. Track, traction, and signalling each keep their own defect lists, in TMS, SMMS, and TDMS, and each asks for a block through BDMS. The Control Office Application holds the corridor. The working timetable is another document. Smart India Hackathon problem statement 26027, from the Ministry of Railways, describes that split as a cause of inefficient block use.

Give the punctuality figures with their population and years. For Mail and Express trains on Indian Railways, Lok Sabha Unstarred Question 3976, answered on 12 August 2026, reports 73.62 percent in 2023–24 and 77.12 percent in 2024–25. The same table gives 77.43 percent for 2025–26. Asset maintenance is one cause the Ministry lists, along with fog, path constraints, alarm-chain pulling, and other events. Do not say punctuality is falling. It rose between those two completed years.

The audit is historical. CAG Report No. 45 of 2017, tabled on 13 March 2018, looked at selected heavy-traffic sections. At the exit conference on 30 August 2017, Railway Board said non-availability of blocks was the main reason deep screening in those observations was overdue. That is not a figure for the network today.

The research question is whether one prototype can keep three judgements separate: an explainable urgency score, a separate forecast of days to a critical severity, and a block window that a person still approves. Rolling Block, notified in the Gazette on 30 November 2023, is the operating idea this lines up with: integrated maintenance planned ahead.

## Slide 3 — method (about 80 seconds)

Start at the five names. They are the feeds the problem statement asks for. In this build they are readers for export files and a locally encoded CSMT–Pune corridor of 192 kilometres. Production endpoints are not connected.

Then the seven steps, in order.

Reading and checking rejects a bad row and keeps the rest of the file.

AI-1 is the report reader. On an ordinary upload it uses local sentence rules. It proposes a finding when the wording matches a known defect phrase, and a second check keeps that finding only when the quoted words are on the page. A person applies the complete rows. It is not a trained generative model, and it does not score urgency.

Formula 1 then scores the open job. Beside it, a gradient-boosting model estimates days until severity reaches 80. Those two numbers stay separate. They are not added into one score.

Formula 2 groups jobs on the same running line and the same power feeder when the next defect is within 3 kilometres.

Google OR-Tools CP-SAT searches for a window that satisfies the hard rules, or it reports that the window is infeasible.

The dashboard shows three policies. A person sanctions, shifts, or rejects.

## Slide 4 — contribution (about 70 seconds)

Do not read every box. A scheduling screen by itself is not the claim. The claim is that the steps stay inspectable and still fit together.

Formula 1 is written down. Safety comes from severity, then overdue days, then whether a temporary speed restriction is already on, then a traffic index for that line. The console shows a score from 0 to 100. The solver can keep the unclipped score so two very severe jobs do not look identical.

The forecast does not replace that score. If asked for a fit number, say the labels are synthetic. On data drawn like the training data the typical error is about one day. On a deliberately shifted synthetic process it is about 3.8 days. Neither figure is field accuracy.

Formula 2 is how repeated mobilisation is reduced: one possession, timed as the longest job plus 15 minutes, instead of one visit per job.

CP-SAT is how that possession is allowed onto the timetable. Rule A keeps a train and a gang out of the same place at the same time. Rule B gives Premium and Superfast trains no induced delay. Rule C caps delay for other train types. In this model the machine pool is one ballast cleaner, two tie tampers, and two tower wagons. A power block also requires isolation. Those fleet caps are prototype settings, not an official count of divisional machines.

Punctuality-First, Asset-First, and Mega-Block change the weights on the same constraints. They are alternatives, not one hidden answer. AI-1 stops when a reviewed record enters the backlog.

## Slide 5 — evaluation (about 80 seconds)

Say the test conditions before the percentage.

The corridor is a 192 kilometre encoded model from CSMT to Pune Junction. The timetable is a 24-hour model of 170 services, not a live control-office feed. The backlog used for this test is 14 encoded demonstration defects, grouped into 11 bundles. The console’s live list starts empty until someone applies a reviewed report. These 14 defects are the demonstration catalog, not a divisional extract.

The chart is Formula 2 on the three clusters that contain more than one defect. Separate job minutes are 150, 180, and 120. Bundled, using the longest job plus 15 minutes, they are 105, 135, and 90. Together that is 450 minutes against 330, so 120 minutes, or 26.7 percent. These are maintenance-work minutes in the model. They are not measured minutes of train delay.

The week result is one simulation, from 24 September 2026 through seven days. Ten of the 14 defects were placed in a feasible window. Four were left for later. Where a night window could not hold a job, the run reported the window infeasible and moved or split the work rather than forcing a train conflict. Do not call this a field week.

## Slide 6 — close (about 60 seconds)

Four sentences are enough.

The prototype shows a path from separate records to a block a person can defend.

The contribution is the split between evidence-backed reading, explainable urgency, a separate forecast, spatial bundling, and constraint solving.

The limits are local demo inputs, a rule-based report reader, and a forecast that has not seen real defect histories.

The next evidence is authorised access, a shadow-mode comparison against today’s block decisions, and a held-out set of real reports.

Point at the four references and stop. Leave the last seconds for the judges.
