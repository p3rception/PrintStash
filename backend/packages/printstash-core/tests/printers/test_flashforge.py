"""The Flashforge client: reading a print's outcome from successive polls.

The printer has no history endpoint, so a finished print is only visible as a
change of ``status`` between two ``/detail`` calls. Two firmware habits make
that harder than mapping one value: a cancel goes ``canceling -> ready`` (and
``canceling`` also flickers during a pause), and ``completed`` can be cleared
on the screen before the next poll. Each test below is one of those sequences.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from printstash_core.printers.flashforge import (
    FlashforgeClient,
    FlashforgeError,
    FlashforgeJobTracker,
    base_url_for,
)
from printstash_core.printers.models import FlashforgeConfig

FILE = "Tweezer_Set-0.16mm_AD5X_PLA_56m27s.gcode.3mf"


def detail(status: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, "printFileName": FILE, **fields}


def run(*details: dict[str, Any]) -> list[dict[str, Any]]:
    tracker = FlashforgeJobTracker()
    return [tracker.observe(d)["print_stats"] for d in details]


def test_completed_reports_complete_with_slicer_grams() -> None:
    stats = run(
        detail("printing", printProgress=0.5, estimatedRightWeight=12.4),
        detail("completed", printProgress=1.0, printDuration=3387),
        detail("ready", printFileName=""),
    )
    assert [s["state"] for s in stats] == ["printing", "complete", "standby"]
    assert stats[1]["filename"] == FILE
    assert stats[1]["filament_used_g"] == 12.4
    assert stats[1]["print_duration"] == 3387


def test_warm_up_with_the_previous_name_opens_no_job() -> None:
    nxt = "Gum_Bottle_AD5X_PLA_2h15m.gcode.3mf"
    stats = run(
        detail("printing", printProgress=0.5),
        detail("completed", printProgress=1.0),
        detail("ready", printFileName=""),
        detail("heating"),
        detail("printing", printFileName=nxt, printProgress=0.01),
    )
    assert "filename" not in stats[3]
    assert stats[4]["filename"] == nxt


def test_cancel_through_canceling_then_ready() -> None:
    stats = run(
        detail("printing", printProgress=0.25, estimatedRightWeight=20.0),
        detail("canceling"),
        detail("ready", printFileName=""),
    )
    assert [s["state"] for s in stats] == ["printing", "printing", "cancelled"]
    assert stats[2]["filename"] == FILE
    assert stats[2]["filament_used_g"] == 5.0


def test_canceling_during_pause_does_not_end_the_print() -> None:
    stats = run(
        detail("printing", printProgress=0.4),
        detail("canceling"),
        detail("pause"),
        detail("printing"),
        detail("completed"),
    )
    assert [s["state"] for s in stats] == [
        "printing",
        "printing",
        "paused",
        "printing",
        "complete",
    ]


def test_missed_completed_is_inferred_from_progress() -> None:
    stats = run(
        detail("printing", printProgress=0.995),
        detail("ready", printFileName=""),
    )
    assert stats[1]["state"] == "complete"


def test_unknown_ending_is_left_to_the_hub() -> None:
    stats = run(
        detail("printing", printProgress=0.3),
        detail("ready", printFileName=""),
    )
    # `standby` with the filename lets the hub record the job as disappeared.
    assert stats[1]["state"] == "standby"
    assert stats[1]["filename"] == FILE


def test_error_holds_the_print_open_until_it_settles() -> None:
    stats = run(
        detail("printing", printProgress=0.6),
        detail("error", errorCode="E0105"),
        detail("ready", printFileName=""),
    )
    assert [s["state"] for s in stats] == ["printing", "paused", "error"]
    assert stats[2]["message"] == "printer_error E0105"


def test_ifs_slots_ignore_current_slot() -> None:
    tracker = FlashforgeJobTracker()
    status = tracker.observe(
        {
            "status": "ready",
            "hasMatlStation": True,
            "matlStationInfo": {
                "currentSlot": 3,
                "slotInfos": [
                    {
                        "slotId": 1,
                        "hasFilament": True,
                        "materialName": "PLA",
                        "materialColor": "#FFFFFF",
                    },
                    {"slotId": 2, "hasFilament": False, "materialName": "PETG"},
                ],
            },
        }
    )
    slots = status["material_slots"]
    assert [(s["slot_key"], s["state"], s["material_type"]) for s in slots] == [
        ("ifs1", "loaded", "PLA"),
        ("ifs2", "empty", None),
    ]


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("192.168.1.132", "http://192.168.1.132:8898"),
        ("192.168.1.132:9000", "http://192.168.1.132:9000"),
        ("http://ad5x.lan/", "http://ad5x.lan:8898"),
    ],
)
def test_base_url_for(host: str, url: str) -> None:
    assert base_url_for(host) == url


@pytest.mark.asyncio
async def test_detail_posts_credentials_and_rejects_bad_code() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if seen[-1]["checkCode"] != "good":
            return httpx.Response(200, json={"code": 1, "message": "check code error"})
        return httpx.Response(200, json={"code": 0, "detail": detail("ready")})

    def client(code: str) -> FlashforgeClient:
        return FlashforgeClient(
            FlashforgeConfig(f"10.9.9.{len(code)}", "SN1", code),
            transport=httpx.MockTransport(handler),
        )

    status = await client("good").query_status()
    assert status["result"]["status"]["print_stats"]["state"] == "standby"
    assert seen[0] == {"serialNumber": "SN1", "checkCode": "good"}
    with pytest.raises(FlashforgeError) as excinfo:
        await client("bad").query_status()
    assert excinfo.value.code == "provider_authentication_failed"
