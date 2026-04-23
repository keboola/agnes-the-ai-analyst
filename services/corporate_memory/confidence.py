"""Confidence scoring for corporate memory facts."""

from datetime import datetime, timezone

# Base confidence lookup: (source_type, detection_type) -> base_confidence
# Use (source_type, None) as fallback when detection_type is irrelevant.
_BASE_CONFIDENCE: dict[tuple[str, str | None], float] = {
    ("user_verification", "correction"): 0.90,
    ("user_verification", "unprompted_definition"): 0.90,
    ("user_verification", "confirmation"): 0.60,
    ("admin_mandate", None): 1.00,
    ("claude_local_md", None): 0.50,
    ("session_transcript", None): 0.50,
}

# Modifier keys and their effects per (source_type, detection_type).
_MODIFIER_EFFECTS: dict[tuple[str, str | None], dict[str, float]] = {
    ("user_verification", "correction"): {"additional_verifiers": 0.05},
    ("user_verification", "unprompted_definition"): {"additional_verifiers": 0.05},
    ("user_verification", "confirmation"): {"admin_confirmed": 0.20},
    ("session_transcript", None): {"user_confirmed_in_session": 0.20},
}


def _lookup_key(source_type: str, detection_type: str | None) -> tuple[str, str | None]:
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
    """Compute confidence score from source/detection type and optional modifiers.

    Modifiers dict supports:
        - additional_verifiers (int): number of extra users verifying the same fact
        - admin_confirmed (bool): whether an admin confirmed the fact
        - user_confirmed_in_session (bool): whether the user confirmed during session
    """
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
    decay_rate_monthly: float = 0.02,
) -> float:
    """Reduce confidence over time. Returns max(0.0, decayed value).

    Decay is linear: confidence - (months_elapsed * decay_rate_monthly).
    """
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    elapsed_seconds = (now - created_at).total_seconds()
    months_elapsed = elapsed_seconds / (30.44 * 24 * 3600)  # average days per month

    decayed = confidence - (months_elapsed * decay_rate_monthly)
    return max(decayed, 0.0)


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
