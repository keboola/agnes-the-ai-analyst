"""JSON schema for LLM structured output from the verification detector."""

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
                    "base_confidence": {"type": "number"},
                },
                "required": [
                    "detection_type",
                    "title",
                    "content",
                    "user_quote",
                    "domain",
                    "entities",
                    "base_confidence",
                ],
            },
        }
    },
    "required": ["verifications"],
}
