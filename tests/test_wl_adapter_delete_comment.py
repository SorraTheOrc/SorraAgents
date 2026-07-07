import json
from plan.wl_adapter import WLAdapter, _normalize_comment_ref, _extract_comments_from_response, _match_comment_id


class DummyWL(WLAdapter):
    def __init__(self, responses):
        # responses is a dict mapping tuple(args) -> stdout
        self.responses = responses

    def _run(self, args):
        key = tuple(args)
        if key in self.responses:
            return self.responses[key]
        # simulate CLI success with empty output
        return json.dumps({})


# ---------------------------------------------------------------------------
# Tests for extracted helper functions
# ---------------------------------------------------------------------------


def test_normalize_comment_ref_short_form():
    """_normalize_comment_ref combines work_id and short comment_id."""
    result = _normalize_comment_ref("SA-TEST-1", "C1")
    assert result == "SA-TEST-1-C1"


def test_normalize_comment_ref_full_form():
    """_normalize_comment_ref returns full ref unchanged."""
    result = _normalize_comment_ref("SA-TEST-1", "SA-TEST-1-C1")
    assert result == "SA-TEST-1-C1"


def test_normalize_comment_ref_full_form_with_sa_prefix():
    """_normalize_comment_ref prepends work_id for full refs from other work items."""
    result = _normalize_comment_ref("SA-TEST-1", "SA-OTHER-2-C1")
    assert result == "SA-TEST-1-SA-OTHER-2-C1"


def test_extract_comments_from_response_top_level():
    """_extract_comments_from_response extracts from top-level comments key."""
    w = {"comments": [{"id": "C1"}, {"id": "C2"}]}
    result = _extract_comments_from_response(w)
    assert len(result) == 2
    assert result[0]["id"] == "C1"


def test_extract_comments_from_response_work_item_key():
    """_extract_comments_from_response extracts from workItem sub-key."""
    w = {"workItem": {"comments": [{"id": "C1"}]}}
    result = _extract_comments_from_response(w)
    assert len(result) == 1
    assert result[0]["id"] == "C1"


def test_extract_comments_from_response_data_key():
    """_extract_comments_from_response extracts from data sub-key."""
    w = {"data": {"items": [{"id": "C1"}]}}
    result = _extract_comments_from_response(w)
    assert len(result) == 1
    assert result[0]["id"] == "C1"


def test_extract_comments_from_response_empty():
    """_extract_comments_from_response returns empty list for empty/None input."""
    assert _extract_comments_from_response({}) == []
    assert _extract_comments_from_response(None) == []


def test_match_comment_id_by_id():
    """_match_comment_id matches by id key."""
    assert _match_comment_id({"id": "C1"}, "C1") is True
    assert _match_comment_id({"id": "C1"}, "C2") is False


def test_match_comment_id_by_comment_id_key():
    """_match_comment_id matches by commentId key."""
    assert _match_comment_id({"commentId": "C1"}, "C1") is True


def test_match_comment_id_by_ref():
    """_match_comment_id matches by ref key with the ref parameter."""
    assert _match_comment_id({"ref": "SA-TEST-1-C1"}, "C1", "SA-TEST-1-C1") is True
    assert _match_comment_id({"reference": "SA-TEST-1-C1"}, "C1", "SA-TEST-1-C1") is True


def test_match_comment_id_no_match():
    """_match_comment_id returns False when no key matches."""
    assert _match_comment_id({"body": "hello"}, "C1") is False
    assert _match_comment_id({}, "C1") is False


# ---------------------------------------------------------------------------
# Integration tests (existing)
# ---------------------------------------------------------------------------


def test_delete_comment_success_and_verify_absent():
    work_id = "SA-TEST-1"
    comment_id = "C1"
    # simulate delete returns some success output, and show later omits comment
    responses = {
        ("comment", "delete", f"{work_id}-{comment_id}"): json.dumps({"success": True}),
        ("show", work_id, "--json"): json.dumps(
            {"workItem": {"id": work_id, "comments": []}}
        ),
    }
    w = DummyWL(responses)
    assert w.delete_comment(work_id, comment_id) is True


def test_delete_comment_missing_still_present():
    work_id = "SA-TEST-2"
    comment_id = "C2"
    # delete reports success but show still shows the comment
    responses = {
        ("comment", "delete", f"{work_id}-{comment_id}"): json.dumps({"success": True}),
        ("show", work_id, "--json"): json.dumps(
            {
                "workItem": {
                    "id": work_id,
                    "comments": [{"id": f"{work_id}-{comment_id}", "body": "hi"}],
                }
            }
        ),
    }
    w = DummyWL(responses)
    # sanity-check the show output before deletion
    shown = w.show(work_id)
    print("DEBUG show output:", shown)
    assert shown is not None
    assert isinstance(shown, dict)
    assert shown.get("workItem") and isinstance(
        shown.get("workItem").get("comments"), list
    )
    # deletion should be considered failed because comment remains
    assert w.delete_comment(work_id, comment_id) is False


def test_delete_comment_cli_failure():
    work_id = "SA-TEST-3"
    comment_id = "C3"

    # simulate delete CLI missing (None) -> failure
    class FailWL(WLAdapter):
        def _run(self, args):
            return None

    w = FailWL()
    assert w.delete_comment(work_id, comment_id) is False
