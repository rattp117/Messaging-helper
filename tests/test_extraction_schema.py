"""AC5: `build_extraction_schema` + the generic `ExtractionResult`
(SPEC-v0.7.md §4 R4, §5 llm/ollama_client.py). Schema shape/size and
`ExtractionResult`'s generic 3-field contract, independent of
`core/parser.py` (module M1) so these are verifiable at the shared-surface
stage without the leaf modules.
"""

from __future__ import annotations

from habit_assistant.config import Config
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.llm.ollama_client import ExtractionResult, build_extraction_schema


def test_build_extraction_schema_default_registry_shape():
    registry = HabitRegistry.from_config(Config())
    schema = build_extraction_schema(registry.category_enum())

    assert schema["type"] == "object"
    assert schema["properties"]["category"]["enum"] == ["water", "stretch", "diary", "unknown"]
    assert schema["properties"]["value"]["type"] == ["number", "string", "boolean", "null"]
    assert schema["properties"]["confidence"]["type"] == "number"
    # SPEC-v1.8.md §2.4/R-B5: `date_offset` is a NEW, OPTIONAL property --
    # present in `properties` but deliberately absent from `required`
    # (AC-9: a backend that ignores it entirely still produces a
    # schema-conformant, byte-identical-to-v1.7 response).
    assert schema["properties"]["date_offset"]["type"] == ["integer", "null"]
    assert set(schema["required"]) == {"category", "value", "confidence"}


def test_build_extraction_schema_size_independent_of_habit_count():
    """AC5: the schema has exactly one `value` field regardless of how
    many habits are configured -- a 30-habit registry produces the same
    number of schema properties as the default 3-habit one."""
    small_schema = build_extraction_schema(["water", "stretch", "diary", "unknown"])
    big_enum = [f"habit_{i}" for i in range(30)] + ["unknown"]
    big_schema = build_extraction_schema(big_enum)

    assert (
        set(small_schema["properties"])
        == set(big_schema["properties"])
        == {"category", "value", "confidence", "date_offset"}
    )
    assert big_schema["properties"]["category"]["enum"] == big_enum


def test_build_extraction_schema_reflects_a_custom_category_enum():
    schema = build_extraction_schema(["sleep", "unknown"])
    assert schema["properties"]["category"]["enum"] == ["sleep", "unknown"]


# ---------------------------------------------------------------------------
# ExtractionResult (SPEC-v0.7.md §5): generic category/value/confidence.
# ---------------------------------------------------------------------------


def test_extraction_result_is_generic_three_field():
    result = ExtractionResult("water", 500.0, 0.9)
    assert result.category == "water"
    assert result.value == 500.0
    assert result.confidence == 0.9


def test_extraction_result_accepts_string_value_for_text():
    result = ExtractionResult("diary", "a quiet day", 0.8)
    assert result.value == "a quiet day"


def test_extraction_result_accepts_bool_value_for_boolean():
    result = ExtractionResult("meds", True, 0.9)
    assert result.value is True


def test_extraction_result_unknown_classmethod():
    result = ExtractionResult.unknown()
    assert result == ExtractionResult("unknown", None, 0.0)
