"""Unit tests for runtime.bfcl_match — the BFCL AST grading rules."""
from __future__ import annotations

from runtime.bfcl_match import match_calls, sanitize_name, standardize_string


def test_sanitize_name():
    assert sanitize_name("spotify.play") == "spotify.play"
    assert sanitize_name("math-func") == "math_func"
    assert sanitize_name("weird name!") == "weird_name_"


def test_standardize_string():
    assert standardize_string("Taylor Swift") == "taylorswift"
    assert standardize_string("St. Mary's, TX.") == 'stmary"stx'


def test_simple_match_pass():
    gt = [{"calc": {"base": [10], "height": [5]}}]
    assert match_calls(gt, [("calc", {"base": 10, "height": 5})]) == []


def test_simple_wrong_function():
    gt = [{"calc": {"base": [10]}}]
    fails = match_calls(gt, [("other", {"base": 10})])
    assert fails and "expected call to 'calc' never fired" in fails[0]


def test_missing_required_param():
    gt = [{"calc": {"base": [10], "height": [5]}}]
    fails = match_calls(gt, [("calc", {"base": 10})])
    assert fails and "missing required param 'height'" in fails[0]


def test_optional_param_omitted():
    # "" in the acceptable list licenses omission (BFCL optional/default).
    gt = [{"calc": {"base": [10], "unit": ["units", ""]}}]
    assert match_calls(gt, [("calc", {"base": 10})]) == []
    assert match_calls(gt, [("calc", {"base": 10, "unit": "units"})]) == []


def test_optional_param_wrong_value():
    gt = [{"calc": {"base": [10], "unit": ["units", ""]}}]
    fails = match_calls(gt, [("calc", {"base": 10, "unit": "parsecs"})])
    assert fails and "unit" in fails[0]


def test_unexpected_param():
    gt = [{"calc": {"base": [10]}}]
    fails = match_calls(gt, [("calc", {"base": 10, "extra": 1})])
    assert fails and "unexpected param 'extra'" in fails[0]


def test_string_standardized_compare():
    gt = [{"play": {"artist": ["Taylor Swift"]}}]
    assert match_calls(gt, [("play", {"artist": "taylor swift"})]) == []
    assert match_calls(gt, [("play", {"artist": "Taylor, Swift."})]) == []


def test_int_promotes_to_float():
    gt = [{"calc": {"x": [10.5]}}]
    assert match_calls(gt, [("calc", {"x": 10.5})]) == []
    fails = match_calls(gt, [("calc", {"x": 11})])
    assert fails and "'x'" in fails[0]


def test_bool_is_not_number():
    gt = [{"f": {"flag": [True]}}]
    assert match_calls(gt, [("f", {"flag": True})]) == []
    fails = match_calls(gt, [("f", {"flag": 1})])
    assert fails


def test_list_elementwise_order_matters():
    gt = [{"f": {"items": [["a", "b"]]}}]
    assert match_calls(gt, [("f", {"items": ["a", "b"]})]) == []
    fails = match_calls(gt, [("f", {"items": ["b", "a"]})])
    assert fails


def test_nested_dict_with_optional_keys():
    gt = [{"order": {"prefs": [{"size": ["large"], "note": ["", "hot"]}]}}]
    assert match_calls(gt, [("order", {"prefs": {"size": "large"}})]) == []
    assert match_calls(
        gt, [("order", {"prefs": {"size": "large", "note": "hot"}})]) == []
    fails = match_calls(gt, [("order", {"prefs": {"note": "hot"}})])
    assert fails


def test_parallel_order_insensitive():
    gt = [{"play": {"artist": ["Taylor Swift"], "duration": [20]}},
          {"play": {"artist": ["Maroon 5"], "duration": [15]}}]
    actual = [("play", {"artist": "Maroon 5", "duration": 15}),
              ("play", {"artist": "Taylor Swift", "duration": 20})]
    assert match_calls(gt, actual) == []


def test_parallel_count_mismatch():
    gt = [{"play": {"artist": ["A"]}}, {"play": {"artist": ["B"]}}]
    fails = match_calls(gt, [("play", {"artist": "A"})])
    assert fails and "expected exactly 2 call(s), got 1" in fails[0]


def test_irrelevance_pass_and_fail():
    assert match_calls([], []) == []
    fails = match_calls([], [("play", {"artist": "A"})])
    assert fails and "irrelevance" in fails[0]


def test_variable_compared_as_string():
    # gt strings, actual non-string → string compare (BFCL "variable" rule)
    gt = [{"f": {"path": ["/tmp/x"]}}]
    assert match_calls(gt, [("f", {"path": "/tmp/x"})]) == []
