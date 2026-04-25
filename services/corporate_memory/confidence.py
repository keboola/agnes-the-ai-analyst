"""Confidence scoring for corporate memory facts."""

from datetime import datetime, timezone

# Module-level config — these are the defaults and can be overridden by
# calling configure() with the corporate_memory.confidence section from instance.yaml.
_BASE_CONFIDENCE: dict = {
    ("user_verification", "correction"): 0.90,
    ("user_verification", "unprompted_definition"): 0.90,
    ("user_verification", "confirmation"): 0.60,
    ("admin_mandate", None): 1.00,
    ("claude_local_md", None): 0.50,
    ("session_transcript", None): 0.50,
}

_MODIFIER_EFFECTS: dict = {
    ("user_verification", "correction"): {"additional_verifiers": 0.05},
    ("user_verification", "unprompted_definition"): {"additional_verifiers": 0.05},
    ("user_verification", "confirmation"): {"admin_confirmed": 0.20},
    ("session_transcript", None): {"user_confirmed_in_session": 0.20},
}

_DECAY_CONFIG: dict = {
    "mode": "exponential",
    "half_life_months": 12,
    "floor": {
        "admin_mandate": 0.50,
        "user_verification": 0.40,
        "default": 0.0,
    },
}


def configure(config: dict) -> None:
    """Override confidence config from the corporate_memory.confidence section of instance.yaml.

    Expected shape (all keys optional, unset keys keep their defaults):
        base:
          user_verification.correction: 0.90
          user_verification.unprompted_definition: 0.90
          user_verification.confirmation: 0.60
          admin_mandate: 1.00
          claude_local_md: 0.50
          session_transcript: 0.50
        modifiers:
          user_verification.correction:
            additional_verifiers: 0.05
          user_verification.confirmation:
            admin_confirmed: 0.20
          session_transcript:
            user_confirmed_in_session: 0.20
        decay:
          mode: exponential         # linear | exponential
          half_life_months: 12      # for exponential
          decay_rate_monthly: 0.02  # for linear
          floor:
            admin_mandate: 0.50
            user_verification: 0.40
            default: 0.0
    """
    global _BASE_CONFIDENCE, _MODIFIER_EFFECTS, _DECAY_CONFIG

    if "base" in config:
        new_base: dict = {}
        for raw_key, value in config["base"].items():
            parts = raw_key.split(".", 1)
            key: tuple = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], None)
            new_base[key] = float(value)
        _BASE_CONFIDENCE = new_base

    if "modifiers" in config:
        new_modifiers: dict = {}
        for raw_key, effects in config["modifiers"].items():
            parts = raw_key.split(".", 1)
            key = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], None)
            new_modifiers[key] = {k: float(v) for k, v in effects.items()}
        _MODIFIER_EFFECTS = new_modifiers

    if "decay" in config:
        _DECAY_CONFIG.update(config["decay"])


def _lookup_key(source_type: str, detection_type: str | None) -> tuple:
    """Resolve the lookup key, falling back to (source_type, None)."""
    key = (source_type, detection_type)
    if key in _BASE_CONFIDENCE:
        return key
    fallback = (source_type, None)
    if fallback in _BASE_CONFIDENCE:
        return fallback
    raise ValueError(f"Unknown source_type={source_type!r}, detection_type={detection_type!r}")


def compute_confidence(
    source_type: str,
    detection_type: str | None = None,
    modifiers: dict | None = None,
) -> float:
    """Compute confidence score from source/detection type and optional modifiers."""
    key = _lookup_key(source_type, detection_type)
    confidence = _BASE_CONFIDENCE[key]

    if modifiers is None:
        return min(confidence, 1.0)

    effects = _MODIFIER_EFFECTS.get(key, {})

    if "additional_verifiers" in effects and "additional_verifiers" in modifiers:
        count = int(modifiers["additional_verifiers"])
        confidence += effects["additional_verifiers"] * count

    if "admin_confirmed" in effects and modifiers.get("admin_confirmed"):
        confidence += effects["admin_confirmed"]

    if "user_confirmed_in_session" in effects and modifiers.get("user_confirmed_in_session"):
        confidence += effects["user_confirmed_in_session"]

    return min(confidence, 1.0)


def apply_decay(
    confidence: float,
    created_at: datetime,
    source_type: str | None = None,
) -> float:
    """Reduce confidence over time using the configured decay model.

    Mode 'exponential' (default): confidence * (0.5 ** (age_months / half_life_months))
    Mode 'linear': confidence - (months_elapsed * decay_rate_monthly)

    Per-source-type floor: admin_mandate defaults to 0.50 (never revoked silently).
    All others default to 0.0. Override via instance.yaml corporate_memory.confidence.decay.floor.
    """
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    elapsed_seconds = (now - created_at).total_seconds()
    months_elapsed = elapsed_seconds / (30.44 * 24 * 3600)

    mode = _DECAY_CONFIG.get("mode", "exponential")
    if mode == "exponential":
        half_life = float(_DECAY_CONFIG.get("half_life_months", 12))
        decayed = confidence * (0.5 ** (months_elapsed / half_life))
    else:
        rate = float(_DECAY_CONFIG.get("decay_rate_monthly", 0.02))
        decayed = confidence - (months_elapsed * rate)

    floor_config = _DECAY_CONFIG.get("floor", {})
    if source_type and source_type in floor_config:
        floor = float(floor_config[source_type])
    else:
        floor = float(floor_config.get("default", 0.0))

    return max(decayed, floor)


def boost_for_multi_verification(
    confidence: float,
    verification_count: int,
    boost_per_user: float = 0.05,
    max_confidence: float = 1.0,
) -> float:
    """Add boost per additional verifier beyond the first. Capped at max_confidence."""
    additional = max(verification_count - 1, 0)
    boosted = confidence + (additional * boost_per_user)
    return min(boosted, max_confidence)
