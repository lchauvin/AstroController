"""Sequence tree flattening from /sequence/json."""

from __future__ import annotations

from astrocontroller.nina.rest import ImageStats, NinaRest, _find_key
from astrocontroller.nina.sequence import parse_sequence

# Shape confirmed against the plugin's own OpenAPI schema (SequenceBaseJson):
# an array of containers with Name/Status/Conditions/Items/Triggers, where
# Items nests both instructions and further containers, plus a GlobalTriggers
# entry that is not a container.
PAYLOAD = [
    {
        "Name": "Start",
        "Status": "FINISHED",
        "Conditions": [],
        "Triggers": [],
        "Items": [
            {"Name": "Cool Camera", "Status": "FINISHED"},
            {"Name": "Unpark Scope", "Status": "FINISHED"},
        ],
    },
    {
        "Name": "Targets",
        "Status": "RUNNING",
        "Conditions": [{"Name": "Loop Until Altitude"}],
        "Triggers": [{"Name": "Meridian Flip"}],
        "Items": [
            {
                "Name": "M31",
                "Status": "RUNNING",
                "Conditions": [{"Name": "Time Condition"}],
                "Items": [
                    {"Name": "Slew and Center", "Status": "FINISHED"},
                    {"Name": "Run Autofocus", "Status": "FINISHED"},
                    {"Name": "Take Exposure Ha", "Status": "RUNNING"},
                    {"Name": "Take Exposure OIII", "Status": "CREATED"},
                ],
            },
            {"Name": "M42", "Status": "CREATED", "Items": []},
        ],
    },
    {"GlobalTriggers": [{"Name": "Autofocus After HFR Increase"}]},
]


def test_nested_items_are_flattened_with_depth():
    tree = parse_sequence(PAYLOAD)
    assert tree.available
    by_name = {s.name: s for s in tree.steps}
    assert by_name["Start"].depth == 0
    assert by_name["M31"].depth == 1
    assert by_name["Take Exposure Ha"].depth == 2


def test_current_step_is_the_deepest_running_leaf():
    # Containers stay RUNNING while their children run, so the innermost
    # running leaf is the step actually executing.
    tree = parse_sequence(PAYLOAD)
    current = tree.current
    assert current is not None
    assert current.name == "Take Exposure Ha"
    assert not current.is_container


def test_containers_are_distinguished_from_instructions():
    tree = parse_sequence(PAYLOAD)
    by_name = {s.name: s for s in tree.steps}
    assert by_name["Targets"].is_container
    assert by_name["M42"].is_container      # empty Items list is still a container
    assert not by_name["Cool Camera"].is_container


def test_parent_links_are_set():
    tree = parse_sequence(PAYLOAD)
    by_name = {s.name: s for s in tree.steps}
    assert by_name["Take Exposure Ha"].parent_id == by_name["M31"].id
    assert by_name["Start"].parent_id is None


def test_progress_counts_leaves_only():
    tree = parse_sequence(PAYLOAD)
    done, total = tree.progress
    # Leaves: Cool Camera, Unpark Scope, Slew and Center, Run Autofocus,
    #         Take Exposure Ha, Take Exposure OIII  -> 6, of which 4 finished.
    assert (done, total) == (4, 6)


def test_global_triggers_are_extracted_not_treated_as_a_step():
    tree = parse_sequence(PAYLOAD)
    assert tree.global_triggers == ["Autofocus After HFR Increase"]
    assert all(s.name != "Autofocus After HFR Increase" for s in tree.steps)


def test_conditions_and_triggers_are_labelled():
    tree = parse_sequence(PAYLOAD)
    targets = next(s for s in tree.steps if s.name == "Targets")
    assert targets.conditions == ["Loop Until Altitude"]
    assert targets.triggers == ["Meridian Flip"]


def test_uninitialised_sequencer_is_not_an_error():
    empty = parse_sequence(None)
    assert not empty.available and empty.error is None
    assert parse_sequence([]).steps == []


def test_no_running_step_yields_no_current():
    tree = parse_sequence([{"Name": "Done", "Status": "FINISHED", "Items": []}])
    assert tree.current is None


# ── REST envelope + stats ──────────────────────────────────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_envelope_is_unwrapped_to_the_payload():
    body = {"Response": {"x": 1}, "Error": "", "StatusCode": 200,
            "Success": True, "Type": "API"}
    assert NinaRest._unwrap("/x", _Resp(body)) == {"x": 1}


def test_unsuccessful_envelope_raises_with_its_message():
    import pytest
    from astrocontroller.nina.rest import NinaError

    body = {"Response": "", "Error": "Sequencer not initialized",
            "StatusCode": 409, "Success": False, "Type": "API"}
    with pytest.raises(NinaError) as exc:
        NinaRest._unwrap("/sequence/json", _Resp(body, 409))
    assert "Sequencer not initialized" in str(exc.value)
    assert exc.value.status == 409


def test_image_stats_parse_the_fields_the_dashboard_needs():
    raw = {
        "ExposureTime": 300.0, "ImageType": "LIGHT", "Filter": "Ha",
        "RmsText": "0.45", "Temperature": -10.0, "CameraName": "ASI2600",
        "Gain": 100, "Offset": 50, "Date": "2026-09-08T22:14:03",
        "TelescopeName": "RC8", "FocalLength": 1624.0, "StDev": 120.5,
        "Mean": 980.2, "Median": 970.0, "Stars": 842, "HFR": 3.21,
        "IsBayered": False, "Min": 100.0, "Max": 65535.0, "HFRStDev": 0.42,
        "TargetName": "M31", "Filename": "D:/Images/M31_Ha_001.fits",
    }
    stats = ImageStats.from_payload(raw, index=7)
    assert stats.index == 7
    assert stats.hfr == 3.21 and stats.stars == 842
    assert stats.max == 65535.0            # saturation check needs this
    assert stats.target == "M31"


def test_image_stats_tolerate_missing_and_null_fields():
    stats = ImageStats.from_payload({"HFR": None, "Stars": "not-a-number"}, index=0)
    assert stats.hfr is None and stats.stars is None


def test_profile_section_is_found_by_name_not_by_path():
    # NINA's profile payload is deeply nested and has moved between plugin
    # versions, so the lookup searches by key name.
    payload = {"A": {"B": {"AstrometrySettings": {"Latitude": 45.5}}}}
    assert _find_key(payload, "AstrometrySettings") == {"Latitude": 45.5}
    assert _find_key({"x": 1}, "AstrometrySettings") is None
