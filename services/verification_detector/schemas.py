"""JSON schema for LLM structured output from the verification detector.

Confidence is intentionally NOT part of this schema. It is derived in code from
(source_type, detection_type) via services.corporate_memory.confidence — the LLM
is not trusted to set its own credibility (see docs/pd-ps-comments.md Q3).
"""

VERIFICATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "detection_type": {
                        "type": "string",
                        "enum": ["correction", "confirmation", "unprompted_definition"],
                    },
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "user_quote": {"type": "string"},
                    "domain": {
                        "type": "string",
                        "enum": [
                            "finance",
                            "engineering",
                            "product",
                            "data",
                            "operations",
                            "infrastructure",
                        ],
                    },
                    "entities": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "detection_type",
                    "title",
                    "content",
                    "user_quote",
                    "domain",
                    "entities",
                ],
            },
        }
    },
    "required": ["verifications"],
}
