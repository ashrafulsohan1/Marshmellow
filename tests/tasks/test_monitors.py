"""Monitor checks — deterministic polling for the four MonitorSpec kinds (M4 Task 1).

A check is a pure function of (baseline, current world state): ``baseline=None``
is the first run, which captures a baseline and never triggers (M4 pinned
convention). HTTP kinds run against ``httpx.MockTransport`` — tests never touch
the network. Summary formats are the M4-pinned spoken reports.
"""

import hashlib

import httpx
import pytest

from marshmellow.core.contracts import MonitorSpec
from marshmellow.tasks.monitors import make_check


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- file_hash (local-only) --------------------------------------------------


def test_file_hash_captures_baseline_then_detects_change(tmp_path) -> None:
    watched = tmp_path / "watched.txt"
    watched.write_bytes(b"hello")
    spec = MonitorSpec(kind="file_hash", target=str(watched), interval_s=60, condition="change")
    check = make_check(spec)

    first = check.check(None)
    assert first.ok and not first.changed
    assert first.value == hashlib.sha256(b"hello").hexdigest()

    watched.write_bytes(b"world")
    second = check.check(first.value)
    assert second.ok and second.changed
    assert second.value == hashlib.sha256(b"world").hexdigest()
    assert second.summary == "the watched file changed"


def test_file_hash_missing_file_is_an_error_not_a_change(tmp_path) -> None:
    spec = MonitorSpec(
        kind="file_hash", target=str(tmp_path / "nope.txt"), interval_s=60, condition="change"
    )
    result = make_check(spec).check("previously-seen-hash")
    assert not result.ok
    assert result.error
    assert not result.changed


# --- url_diff ----------------------------------------------------------------


def test_url_diff_hashes_body_and_detects_change() -> None:
    bodies = iter([b"v1", b"v2"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(bodies))

    spec = MonitorSpec(
        kind="url_diff", target="https://example.com/page", interval_s=60, condition="change"
    )
    check = make_check(spec, client=_mock_client(handler))

    first = check.check(None)
    assert first.ok and not first.changed
    assert first.value == hashlib.sha256(b"v1").hexdigest()

    second = check.check(first.value)
    assert second.ok and second.changed
    assert second.value == hashlib.sha256(b"v2").hexdigest()
    assert second.summary == "the watched page changed"


def test_url_diff_non_2xx_is_an_error_not_a_change() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"gone")

    spec = MonitorSpec(
        kind="url_diff", target="https://example.com/page", interval_s=60, condition="change"
    )
    result = make_check(spec, client=_mock_client(handler)).check("previously-seen-hash")
    assert not result.ok
    assert result.error
    assert not result.changed


# --- api_field ---------------------------------------------------------------


def test_api_field_parses_hash_path_walks_dotted_field_and_strips_fragment() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"build": {"version": "1.2.3"}})

    spec = MonitorSpec(
        kind="api_field",
        target="https://api.example.com/status#build.version",
        interval_s=60,
        condition="version bump",
    )
    check = make_check(spec, client=_mock_client(handler))

    first = check.check(None)
    assert first.ok and not first.changed
    assert first.value == "1.2.3"
    assert captured == ["https://api.example.com/status"]  # #dotted.path never sent

    second = check.check("1.2.2")
    assert second.ok and second.changed
    assert second.summary == "build.version is now 1.2.3"


def test_api_field_missing_field_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"build": {}})

    spec = MonitorSpec(
        kind="api_field",
        target="https://api.example.com/status#build.version",
        interval_s=60,
        condition="version bump",
    )
    result = make_check(spec, client=_mock_client(handler)).check("1.2.2")
    assert not result.ok
    assert result.error
    assert not result.changed


def test_api_field_non_2xx_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    spec = MonitorSpec(
        kind="api_field",
        target="https://api.example.com/status#build.version",
        interval_s=60,
        condition="version bump",
    )
    result = make_check(spec, client=_mock_client(handler)).check("1.2.2")
    assert not result.ok
    assert result.error


def test_api_field_target_without_hash_path_is_rejected() -> None:
    spec = MonitorSpec(
        kind="api_field", target="https://api.example.com/status", interval_s=60, condition="c"
    )
    with pytest.raises(ValueError, match="dotted"):
        make_check(spec)


# --- rss ---------------------------------------------------------------------


def _feed(items: list[tuple[str, str]]) -> bytes:
    entries = "\n".join(
        f"<item><title>{title}</title><guid>{guid}</guid></item>" for guid, title in items
    )
    return (
        '<?xml version="1.0"?>\n<rss version="2.0"><channel><title>Channel</title>'
        f"{entries}</channel></rss>"
    ).encode()


def test_rss_detects_new_entry_and_names_the_title() -> None:
    feed_one = _feed([("id-1", "First video")])
    feed_two = _feed([("id-2", "Second video"), ("id-1", "First video")])
    responses = iter([feed_one, feed_two])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(responses))

    spec = MonitorSpec(
        kind="rss", target="https://example.com/feed.xml", interval_s=300, condition="new upload"
    )
    check = make_check(spec, client=_mock_client(handler))

    first = check.check(None)
    assert first.ok and not first.changed
    assert first.value == "id-1"  # newest entry id captures the baseline

    second = check.check(first.value)
    assert second.ok and second.changed
    assert second.value == "id-2"
    assert second.summary == "new upload: Second video"


def test_rss_feed_without_entries_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        empty = b"<rss version='2.0'><channel><title>x</title></channel></rss>"
        return httpx.Response(200, content=empty)

    spec = MonitorSpec(
        kind="rss", target="https://example.com/feed.xml", interval_s=300, condition="new upload"
    )
    result = make_check(spec, client=_mock_client(handler)).check(None)
    assert not result.ok
    assert result.error


# --- make_check dispatch -----------------------------------------------------


def test_make_check_rejects_unknown_kind() -> None:
    # model_construct skips Literal validation, simulating a spec from an older build
    spec = MonitorSpec.model_construct(
        kind="carrier_pigeon", target="loft", interval_s=60, condition="coo"
    )
    with pytest.raises(ValueError, match="kind"):
        make_check(spec)
