# Escalation forecast

The model in `ml_priority_model.joblib` is trained on labels from the documented synthetic degradation rule in `ml_priority.py`. Those labels are not real TMS, SMMS, or TDMS defect history. For each synthetic row the rule computes how many days severity would take to reach 80, then adds seeded observation noise. The saved regressor is a secondary forecast of that quantity. It does not replace the Formula 1 urgency score.

## Held-out numbers

Both checks use 2,000 rows. Covariates are drawn the same way in each: severity as an integer from 1 to 100, days overdue as an integer from 0 to 40, defect type uniform over the six types, and a TSR present or absent with equal chance. What changes is the label-generating rule. The ranges below were fixed before the scores were computed and were not adjusted afterward.

**Matched hold-out.** Training and the 400-row hold-out (seed 42, 80/20 split) draw, on every row, a noise standard deviation from 0.15 to 0.90 day, a TSR multiplier from 1.40 to 1.90, and an overdue acceleration from 0.030 to 0.060. On that hold-out the model scores **R² = 0.98** and **MAE = 0.966 days** (printed values R² = 0.9798, MAE = 0.966 days).

**Shifted distribution.** A separate set (seed 7, all 2,000 rows scored) draws a noise standard deviation from 1.25 to 2.50, a TSR multiplier from 2.20 to 2.80, and an overdue acceleration from 0.100 to 0.160. These ranges do not overlap the training ranges. On that set the model scores **R² = 0.28** and **MAE = 3.84 days** (printed values R² = 0.2772, MAE = 3.843 days).

## What the gap means

Inside the degradation dynamics it was trained on, the model interpolates reliably: a hold-out drawn from the same ranges is off by about a day. Outside that regime the error jumps to nearly four days and most of the variance is unexplained. The forecast does not extrapolate to a fundamentally different degradation process. That is why the explainable Formula 1 score, not this model, is the safety-critical scheduling input, and why this model is a secondary forecast only.

## When real history is available

`data_ingestion.py` already turns TMS, SMMS, and TDMS exports into `TrackDefect` records for the live backlog. It does not yet carry the outcome this model needs: how a defect's severity actually moved, and when it reached a critical condition. Once that history is ingested, training labels would be those observed days, the same feature columns would be fit to measurements instead of the synthetic daily-gain rule, and the generalization check would be a later period or another section rather than a hand-set shift of the synthetic coefficients. Until those outcomes exist, the planner keeps using Formula 1 to decide what is scheduled.
