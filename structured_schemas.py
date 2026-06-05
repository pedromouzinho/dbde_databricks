"""
Schemas JSON para Structured Outputs do Azure OpenAI.
Usados quando precisamos de respostas em formato previsível.
"""

SPRINT_ANALYSIS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "sprint_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "sprint_name": {"type": "string"},
                "total_items": {"type": "integer"},
                "completed": {"type": "integer"},
                "in_progress": {"type": "integer"},
                "blocked": {"type": "integer"},
                "velocity": {"type": "number"},
                "health": {"type": "string", "enum": ["healthy", "at_risk", "critical"]},
                "summary": {"type": "string"},
                "risks": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "sprint_name",
                "total_items",
                "completed",
                "in_progress",
                "blocked",
                "velocity",
                "health",
                "summary",
                "risks",
                "recommendations",
            ],
            "additionalProperties": False,
        },
    },
}

EMAIL_CLASSIFICATION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "email_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["urgent", "action_required", "informational", "fyi", "spam"],
                },
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "summary": {"type": "string"},
                "suggested_action": {"type": "string"},
                "requires_response": {"type": "boolean"},
            },
            "required": ["category", "priority", "summary", "suggested_action", "requires_response"],
            "additionalProperties": False,
        },
    },
}

DOCUMENT_ENTITIES_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "document_entities",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "document_type": {"type": "string"},
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "value": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["type", "value", "confidence"],
                        "additionalProperties": False,
                    },
                },
                "key_dates": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
            "required": ["document_type", "entities", "key_dates", "summary"],
            "additionalProperties": False,
        },
    },
}

USER_STORY_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "user_story",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "as_a": {"type": "string"},
                "i_want": {"type": "string"},
                "so_that": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "test_scenarios": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "given": {"type": "string"},
                            "when": {"type": "string"},
                            "then": {"type": "string"},
                        },
                        "required": ["given", "when", "then"],
                        "additionalProperties": False,
                    },
                },
                "story_points": {"type": "integer"},
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            },
            "required": [
                "title",
                "as_a",
                "i_want",
                "so_that",
                "acceptance_criteria",
                "test_scenarios",
                "story_points",
                "priority",
            ],
            "additionalProperties": False,
        },
    },
}

SPEECH_PROMPT_NORMALIZATION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "speech_prompt_normalization",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "normalized_prompt": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "inferred_mode": {"type": "string", "enum": ["general", "userstory"]},
                "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["normalized_prompt", "confidence", "inferred_mode", "notes"],
            "additionalProperties": False,
        },
    },
}

DATA_TABLE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "data_table",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                "total_rows": {"type": "integer"},
            },
            "required": ["title", "columns", "rows", "total_rows"],
            "additionalProperties": False,
        },
    },
}

# Schema específico já alinhado com output esperado da tool screenshot_to_us.
SCREENSHOT_USER_STORIES_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "screenshot_user_stories",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "stories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "provenance": {"type": "string"},
                            "conditions": {"type": "array", "items": {"type": "string"}},
                            "composition_and_behavior": {"type": "array", "items": {"type": "string"}},
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["id", "text"],
                                    "additionalProperties": False,
                                },
                            },
                            "test_scenarios": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "category": {"type": "string"},
                                        "preconditions": {"type": "string"},
                                        "test_data": {"type": "string"},
                                        "steps": {"type": "array", "items": {"type": "string"}},
                                        "covers": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": [
                                        "id",
                                        "title",
                                        "category",
                                        "preconditions",
                                        "test_data",
                                        "steps",
                                        "covers",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                            "test_data": {"type": "array", "items": {"type": "string"}},
                            "observations": {"type": "array", "items": {"type": "string"}},
                            "clarification_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "title",
                            "description",
                            "provenance",
                            "conditions",
                            "composition_and_behavior",
                            "acceptance_criteria",
                            "test_scenarios",
                            "test_data",
                            "observations",
                            "clarification_questions",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["stories"],
            "additionalProperties": False,
        },
    },
}

USER_STORY_LANE_DRAFT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "user_story_lane_draft",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "narrative": {
                    "type": "object",
                    "properties": {
                        "as_a": {"type": "string"},
                        "i_want": {"type": "string"},
                        "so_that": {"type": "string"},
                    },
                    "required": ["as_a", "i_want", "so_that"],
                    "additionalProperties": False,
                },
                "business_goal": {"type": "string"},
                "provenance": {"type": "array", "items": {"type": "string"}},
                "conditions": {"type": "array", "items": {"type": "string"}},
                "rules_constraints": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                },
                "test_scenarios": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "category": {"type": "string"},
                            "preconditions": {"type": "string"},
                            "test_data": {"type": "string"},
                            "given": {"type": "string"},
                            "when": {"type": "string"},
                            "then": {"type": "string"},
                            "covers": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "id",
                            "title",
                            "category",
                            "preconditions",
                            "test_data",
                            "given",
                            "when",
                            "then",
                            "covers",
                        ],
                        "additionalProperties": False,
                    },
                },
                "dependencies": {"type": "array", "items": {"type": "string"}},
                "observations": {"type": "array", "items": {"type": "string"}},
                "clarification_questions": {"type": "array", "items": {"type": "string"}},
                "source_keys": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": [
                "title",
                "narrative",
                "business_goal",
                "provenance",
                "conditions",
                "rules_constraints",
                "acceptance_criteria",
                "test_scenarios",
                "dependencies",
                "observations",
                "clarification_questions",
                "source_keys",
                "confidence",
            ],
            "additionalProperties": False,
        },
    },
}
