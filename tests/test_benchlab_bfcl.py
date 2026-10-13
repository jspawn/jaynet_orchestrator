"""BFCL importer: row→case conversion, schema mapping, category fetching
(mocked HTTP), and the write_cases validation roundtrip."""
from __future__ import annotations

import pytest

from plugins.benchlab import importer
from runtime.eval_cases import validate_case_dict

_SIMPLE_ROW = {
    "id": "simple_python_0",
    "question": [[{"role": "user",
                   "content": "Find the area of a triangle with base 10 and "
                              "height 5."}]],
    "function": [{
        "name": "calculate_triangle_area",
        "description": "Compute the area of a triangle.",
        "parameters": {"type": "dict",
                       "properties": {
                           "base": {"type": "integer",
                                    "description": "base length"},
                           "height": {"type": "float"},
                           "unit": {"type": "string", "enum": ["units"]},
                           "tags": {"type": "array",
                                    "items": {"type": "string"}},
                           "meta": {"type": "dict"}},
                       "required": ["base", "height"]}}],
}
_SIMPLE_GT = [{"calculate_triangle_area": {"base": [10], "height": [5],
                                           "unit": ["units", ""]}}]


def test_row_to_case_simple():
    case = importer.bfcl_row_to_case("simple_python", _SIMPLE_ROW, _SIMPLE_GT)
    assert case["id"] == "bfcl-simple-python-0"
    assert case["judge"] is False
    assert case["tags"] == ["bench", "bfcl", "bfcl-simple-python"]
    assert case["turns"] == [{"user": "Find the area of a triangle with "
                                     "base 10 and height 5."}]
    assert case["stub_tools"][0]["name"] == "bfcl.calculate_triangle_area"
    assert case["expect"]["tool_calls"] == [
        {"bfcl.calculate_triangle_area": {"base": [10], "height": [5],
                                          "unit": ["units", ""]}}]
    # the produced case must survive the real validator
    assert validate_case_dict(case["id"], case) == []


def test_schema_type_mapping():
    case = importer.bfcl_row_to_case("simple_python", _SIMPLE_ROW, _SIMPLE_GT)
    props = case["stub_tools"][0]["parameters"]["properties"]
    assert props["base"]["type"] == "integer"
    assert props["height"]["type"] == "number"          # float → number
    assert props["unit"]["enum"] == ["units"]
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["meta"]["type"] == "object"            # dict → object
    assert case["stub_tools"][0]["parameters"]["required"] == ["base",
                                                               "height"]


def test_dotted_function_name_becomes_underscore():
    row = {"id": "parallel_0",
           "question": [[{"role": "user", "content": "play two songs"}]],
           "function": [{"name": "spotify.play", "description": "d",
                         "parameters": {"type": "dict", "properties": {},
                                        "required": []}}]}
    gt = [{"spotify.play": {"artist": ["A"]}},
          {"spotify.play": {"artist": ["B"]}}]
    case = importer.bfcl_row_to_case("parallel", row, gt)
    assert case["stub_tools"][0]["name"] == "bfcl.spotify_play"
    assert case["expect"]["tool_calls"] == [
        {"bfcl.spotify_play": {"artist": ["A"]}},
        {"bfcl.spotify_play": {"artist": ["B"]}}]
    assert validate_case_dict(case["id"], case) == []


def test_irrelevance_row_no_calls_expected():
    row = {"id": "irrelevance_0",
           "question": [[{"role": "user", "content": "how are you?"}]],
           "function": [{"name": "math.sqrt", "description": "d",
                         "parameters": {"type": "dict", "properties": {},
                                        "required": []}}]}
    case = importer.bfcl_row_to_case("irrelevance", row, None)
    assert case["expect"]["tool_calls"] == []
    assert case["stub_tools"]           # the tempting function IS offered
    assert validate_case_dict(case["id"], case) == []


def test_multi_turn_row_skipped():
    row = {"id": "multi_0",
           "question": [[{"role": "user", "content": "one"}],
                        [{"role": "user", "content": "two"}]],
           "function": []}
    with pytest.raises(importer.SkipTask):
        importer.bfcl_row_to_case("simple_python", row, [])


def test_bfcl_cases_limit_and_missing_gt(monkeypatch):
    rows = [dict(_SIMPLE_ROW, id=f"simple_python_{i}") for i in range(3)]
    answers = {"simple_python_0": _SIMPLE_GT,
               "simple_python_1": _SIMPLE_GT}
    # simple_python_2 deliberately has no ground truth → skipped, not
    # silently graded as irrelevance
    monkeypatch.setattr(importer, "fetch_bfcl_category",
                        lambda cat: {"rows": rows, "answers": answers})
    cases, skipped = importer.bfcl_cases(["simple_python"], limit=10)
    assert [c["id"] for c in cases] == ["bfcl-simple-python-0",
                                        "bfcl-simple-python-1"]
    assert skipped and "no ground truth" in skipped[0]["reason"]
    # limit caps the rows
    cases, _ = importer.bfcl_cases(["simple_python"], limit=1)
    assert len(cases) == 1


def test_write_cases_roundtrip(tmp_path):
    case = importer.bfcl_row_to_case("simple_python", _SIMPLE_ROW, _SIMPLE_GT)
    result = importer.write_cases([case], tmp_path)
    assert result["imported"] == ["bfcl-simple-python-0"]
    assert not result["failed"]
    text = (tmp_path / "bfcl-simple-python-0.yaml").read_text()
    assert "judge: false" in text and "tool_calls" in text
