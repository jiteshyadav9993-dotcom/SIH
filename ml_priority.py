"""
ml_priority.py
====================================================================
Synthetic escalation forecast for a TrackDefect.

This module is deliberately separate from Formula 1 in defects.py.
`compute_priority_urgency_score` is not called and is not modified.

The training label is synthetic. It is the number of days a simple,
documented degradation rule needs for severity to reach 80, plus a
small seeded observation noise. The fitted regressor is written to
`ml_priority_model.joblib` and loaded from that file on prediction,
so importing this module does not retrain.
====================================================================
"""

from __future__ import annotations

import random
from pathlib import Path

try:
    import joblib
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.model_selection import train_test_split

    ML_AVAILABLE = True
except Exception:
    joblib = None  # type: ignore[assignment]
    GradientBoostingRegressor = None  # type: ignore[assignment,misc]
    mean_absolute_error = None  # type: ignore[assignment]
    r2_score = None  # type: ignore[assignment]
    train_test_split = None  # type: ignore[assignment]
    ML_AVAILABLE = False

from defects import DefectType

# ---------------------------------------------------------------------------
# Synthetic degradation rule (not field measurements)
# ---------------------------------------------------------------------------
#
# A defect's severity climbs by a fixed number of points per day until it
# hits CRITICAL_SEVERITY (80). The daily gain is:
#
#   daily_gain = base_rate[defect_type]
#                * (TSR_RATE_MULTIPLIER if a TSR is already in force else 1)
#                * (1 + OVERDUE_ACCELERATION_PER_DAY * days_overdue)
#
#   days_to_critical = max(0, (CRITICAL_SEVERITY - severity) / daily_gain)
#
# Base rates (severity points per day, before TSR / overdue acceleration):
#   RAIL_FRACTURE and OHE_SAG climb fastest — an open fracture and a sagging
#   contact wire are treated as already unstable.
#   TAMPING_REQUIRED climbs slowest — geometry faults worsen gradually.
#   The other three types sit between those two extremes.
#
# A TSR multiplies the daily gain (the defect is already affecting running).
# Each day already overdue adds 4.5% to the daily gain (neglected defects
# worsen faster). Severity at or above 80 is already critical, so the label
# is 0 before noise is applied.
#
# Training labels then add Gaussian noise and are clipped at 0. The original
# generator uses a fixed std of 0.35 day. The varied generator, which
# train_and_save fits, draws that std and the two rule coefficients from
# ranges around the documented values. The noise is part of the synthetic
# generator only; live predictions are the regressor's output, clipped at 0.

CRITICAL_SEVERITY: float = 80.0
TSR_RATE_MULTIPLIER: float = 1.65
OVERDUE_ACCELERATION_PER_DAY: float = 0.045
LABEL_NOISE_STD_DAYS: float = 0.35
N_SAMPLES: int = 2000
RANDOM_SEED: int = 42

# Ranges used when fitting. Each sample draws its own noise std and its own
# copy of the two rule coefficients. The documented constants sit inside
# these ranges. They are not the only values the labels see.
VARIED_NOISE_STD_RANGE: tuple[float, float] = (0.15, 0.90)
VARIED_TSR_MULTIPLIER_RANGE: tuple[float, float] = (1.40, 1.90)
VARIED_OVERDUE_ACCEL_RANGE: tuple[float, float] = (0.030, 0.060)

# Held-out shift. These ranges do not overlap the training ranges. They were
# chosen as a harder measurement process, not adjusted after seeing the score.
SHIFTED_NOISE_STD_RANGE: tuple[float, float] = (1.25, 2.50)
SHIFTED_TSR_MULTIPLIER_RANGE: tuple[float, float] = (2.20, 2.80)
SHIFTED_OVERDUE_ACCEL_RANGE: tuple[float, float] = (0.10, 0.16)
SHIFTED_RANDOM_SEED: int = 7

BASE_DAILY_SEVERITY_GAIN: dict[DefectType, float] = {
    DefectType.RAIL_FRACTURE: 4.5,
    DefectType.OHE_SAG: 3.8,
    DefectType.WELD_FAILURE: 2.4,
    DefectType.SIGNAL_INTERLOCK: 1.8,
    DefectType.DEEP_SCREENING: 1.4,
    DefectType.TAMPING_REQUIRED: 0.9,
}

# Column order of the design matrix. One boolean column per DefectType.
DEFECT_TYPE_ORDER: tuple[DefectType, ...] = (
    DefectType.RAIL_FRACTURE,
    DefectType.WELD_FAILURE,
    DefectType.OHE_SAG,
    DefectType.SIGNAL_INTERLOCK,
    DefectType.TAMPING_REQUIRED,
    DefectType.DEEP_SCREENING,
)

FEATURE_NAMES: tuple[str, ...] = (
    "severity",
    "days_overdue",
    "has_tsr",
    *(f"type_{defect_type.value}" for defect_type in DEFECT_TYPE_ORDER),
)

MODEL_PATH: Path = Path(__file__).resolve().parent / "ml_priority_model.joblib"

_CACHED_MODEL: GradientBoostingRegressor | None = None
_DOCUMENTED_RULE_WARNING_EMITTED: bool = False


def _days_to_critical(
    severity: float,
    days_overdue: float,
    has_tsr: bool,
    defect_type: DefectType,
    tsr_rate_multiplier: float,
    overdue_acceleration_per_day: float,
) -> float:
    """Days until severity would hit 80 under one setting of the synthetic rule."""
    gap = CRITICAL_SEVERITY - float(severity)
    if gap <= 0.0:
        return 0.0
    daily_gain = (
        BASE_DAILY_SEVERITY_GAIN[defect_type]
        * (tsr_rate_multiplier if has_tsr else 1.0)
        * (1.0 + overdue_acceleration_per_day * max(0.0, float(days_overdue)))
    )
    return gap / daily_gain


def synthetic_days_to_critical(
    severity: float,
    days_overdue: float,
    has_tsr: bool,
    defect_type: DefectType,
) -> float:
    """Days until severity would hit 80 under the documented synthetic rule, before noise."""
    return _days_to_critical(
        severity,
        days_overdue,
        has_tsr,
        defect_type,
        TSR_RATE_MULTIPLIER,
        OVERDUE_ACCELERATION_PER_DAY,
    )


def _feature_row(
    severity: float,
    days_overdue: float,
    has_tsr: bool,
    defect_type: DefectType,
) -> list[float]:
    one_hot = [1.0 if defect_type is item else 0.0 for item in DEFECT_TYPE_ORDER]
    return [
        float(severity),
        float(days_overdue),
        1.0 if has_tsr else 0.0,
        *one_hot,
    ]


def _draw_range(rng: random.Random, bounds: tuple[float, float]) -> float:
    low, high = bounds
    if high < low:
        raise ValueError(f"Range upper bound {high} is below lower bound {low}")
    return rng.uniform(low, high)


def generate_varied_synthetic_training_data(
    n_samples: int = N_SAMPLES,
    random_seed: int = RANDOM_SEED,
    *,
    noise_std_range: tuple[float, float] = VARIED_NOISE_STD_RANGE,
    tsr_multiplier_range: tuple[float, float] = VARIED_TSR_MULTIPLIER_RANGE,
    overdue_accel_range: tuple[float, float] = VARIED_OVERDUE_ACCEL_RANGE,
) -> tuple[list[list[float]], list[float]]:
    """
    Draw severity, days overdue, defect type, and a TSR flag, then label each row
    from its own noise draw.

    Each row draws its own observation-noise std and its own TSR multiplier
    and overdue acceleration, then labels the row from that draw.
    """
    if n_samples < 2:
        raise ValueError(f"n_samples must be at least 2, got {n_samples}")
    rng = random.Random(random_seed)
    features: list[list[float]] = []
    labels: list[float] = []
    for _ in range(n_samples):
        severity = rng.randint(1, 100)
        days_overdue = rng.randint(0, 40)
        defect_type = rng.choice(DEFECT_TYPE_ORDER)
        has_tsr = rng.choice((True, False))
        noise_std = _draw_range(rng, noise_std_range)
        tsr_multiplier = _draw_range(rng, tsr_multiplier_range)
        overdue_accel = _draw_range(rng, overdue_accel_range)
        label = _days_to_critical(
            severity,
            days_overdue,
            has_tsr,
            defect_type,
            tsr_multiplier,
            overdue_accel,
        )
        if label > 0.0:
            label = max(0.0, label + rng.gauss(0.0, noise_std))
        features.append(_feature_row(severity, float(days_overdue), has_tsr, defect_type))
        labels.append(label)
    return features, labels


def train_and_save(
    n_samples: int = N_SAMPLES,
    random_seed: int = RANDOM_SEED,
    model_path: Path = MODEL_PATH,
) -> dict[str, float]:
    """
    Fit a gradient-boosting regressor on the varied synthetic labels.

    Prints R² and MAE on the matched hold-out split and on a second set
    drawn from SHIFTED_* ranges. The saved payload still stores the matched
    r2 and mae under those keys. feature_names stay FEATURE_NAMES.
    """
    global _CACHED_MODEL
    features, labels = generate_varied_synthetic_training_data(n_samples, random_seed)
    features_train, features_test, labels_train, labels_test = train_test_split(
        features,
        labels,
        test_size=0.2,
        random_state=random_seed,
    )
    model = GradientBoostingRegressor(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.08,
        random_state=random_seed,
    )
    model.fit(features_train, labels_train)
    predictions = model.predict(features_test)
    r2 = float(r2_score(labels_test, predictions))
    mae = float(mean_absolute_error(labels_test, predictions))
    shifted_features, shifted_labels = generate_varied_synthetic_training_data(
        n_samples,
        SHIFTED_RANDOM_SEED,
        noise_std_range=SHIFTED_NOISE_STD_RANGE,
        tsr_multiplier_range=SHIFTED_TSR_MULTIPLIER_RANGE,
        overdue_accel_range=SHIFTED_OVERDUE_ACCEL_RANGE,
    )
    shifted_predictions = model.predict(shifted_features)
    shifted_r2 = float(r2_score(shifted_labels, shifted_predictions))
    shifted_mae = float(mean_absolute_error(shifted_labels, shifted_predictions))
    print(
        "Matched hold-out (same varied distribution as training, "
        f"n={n_samples}, test={len(labels_test)}): R2={r2:.4f}  MAE={mae:.3f} days"
    )
    print(
        "Shifted distribution "
        f"(noise std {SHIFTED_NOISE_STD_RANGE[0]:.2f}-{SHIFTED_NOISE_STD_RANGE[1]:.2f}, "
        f"TSR multiplier {SHIFTED_TSR_MULTIPLIER_RANGE[0]:.2f}-{SHIFTED_TSR_MULTIPLIER_RANGE[1]:.2f}, "
        f"overdue accel {SHIFTED_OVERDUE_ACCEL_RANGE[0]:.3f}-{SHIFTED_OVERDUE_ACCEL_RANGE[1]:.3f}, "
        f"n={n_samples}, seed={SHIFTED_RANDOM_SEED}): "
        f"R2={shifted_r2:.4f}  MAE={shifted_mae:.3f} days"
    )
    payload = {
        "model": model,
        "feature_names": FEATURE_NAMES,
        "defect_type_order": [item.value for item in DEFECT_TYPE_ORDER],
        "r2": r2,
        "mae": mae,
        "shifted_r2": shifted_r2,
        "shifted_mae": shifted_mae,
        "n_samples": n_samples,
        "random_seed": random_seed,
    }
    joblib.dump(payload, model_path)
    print(f"Saved model to {model_path}")
    _CACHED_MODEL = model
    return {
        "r2": r2,
        "mae": mae,
        "shifted_r2": shifted_r2,
        "shifted_mae": shifted_mae,
    }


def load_model(model_path: Path = MODEL_PATH) -> GradientBoostingRegressor:
    """Load the saved regressor. Trains and writes the file only when it is missing."""
    global _CACHED_MODEL
    if _CACHED_MODEL is not None and model_path == MODEL_PATH:
        return _CACHED_MODEL
    if not model_path.is_file():
        train_and_save(model_path=model_path)
        if _CACHED_MODEL is None:
            raise RuntimeError(f"Training did not produce a model at {model_path}")
        return _CACHED_MODEL
    payload = joblib.load(model_path)
    model = payload["model"]
    if model_path == MODEL_PATH:
        _CACHED_MODEL = model
    return model


def _coerce_defect_type(raw: DefectType | str) -> DefectType:
    if isinstance(raw, DefectType):
        return raw
    try:
        return DefectType(str(raw))
    except ValueError as exc:
        known = ", ".join(item.value for item in DEFECT_TYPE_ORDER)
        raise ValueError(f"Unknown defect_type {raw!r}; expected one of {known}") from exc


def _documented_rule_days(
    severity: float,
    days_overdue: float,
    has_tsr: bool,
    defect_type: DefectType,
) -> float:
    """Documented synthetic rule, with one warning the first time the model is unused."""
    global _DOCUMENTED_RULE_WARNING_EMITTED
    if not _DOCUMENTED_RULE_WARNING_EMITTED:
        print(
            "Warning: the trained model could not be loaded and the documented rule is being used."
        )
        _DOCUMENTED_RULE_WARNING_EMITTED = True
    return synthetic_days_to_critical(severity, days_overdue, has_tsr, defect_type)


def predict_days_to_critical(defect: "defects.TrackDefect") -> float:
    """Predicted days until this defect's severity would reach 80."""
    defect_type = _coerce_defect_type(defect.defect_type)
    severity = float(defect.severity)
    days_overdue = float(defect.days_overdue)
    has_tsr = bool(defect.has_tsr)
    if not ML_AVAILABLE:
        return _documented_rule_days(severity, days_overdue, has_tsr, defect_type)
    row = _feature_row(severity, days_overdue, has_tsr, defect_type)
    try:
        prediction = float(load_model().predict([row])[0])
    except Exception:
        return _documented_rule_days(severity, days_overdue, has_tsr, defect_type)
    return max(0.0, prediction)


if __name__ == "__main__":
    train_and_save()
