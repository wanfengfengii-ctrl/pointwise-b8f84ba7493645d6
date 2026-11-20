import json
from pathlib import Path
import sys

from station.device.fixture import DeliverySchedule, OpticalFixture, OpticalObservation


def check_fixture(data):
    scenario_ids = set()
    for case in data["cases"]:
        name = case["id"]
        if name in scenario_ids:
            raise ValueError(f"duplicate scenario {name}")
        scenario_ids.add(name)
        observations = tuple(OpticalObservation(**row) for row in case["observations"])
        OpticalFixture(observations)
        expected_groups = {(index, "evidence") for index in range(len(observations))}
        expected_groups.update((index, "raw") for index, obs in enumerate(observations)
                               if obs.kind == "reading")
        schedule = DeliverySchedule()
        queued = set()
        for slot, step in enumerate(case["steps"], 1):
            for index, channel in step["deliver"]:
                group = (index, channel)
                if type(index) is not int or group not in expected_groups or group in queued:
                    raise ValueError(f"{name}: invalid or repeated delivery group {group!r}")
                schedule.schedule(slot, group)
                queued.add(group)
        if queued != expected_groups or not case["steps"]:
            raise ValueError(f"{name}: incomplete delivery schedule")
        for slot, step in enumerate(case["steps"], 1):
            due = schedule.take_due(slot)
            if due != tuple(map(tuple, step["deliver"])):
                raise RuntimeError(f"{name}: delivery order changed at step {slot}")
            raw = [observations[index] for index, channel in due if channel == "raw"]
            print(json.dumps({"case": name, "step": slot,
                              "raw_arrivals": [[e.head, e.tick, e.raw_code] for e in raw],
                              "expected_after_delivery": step["expect"]}, sort_keys=True))
    if not scenario_ids:
        raise ValueError("fixture has no scenarios")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_first_read_fixture.py FIXTURE")
    check_fixture(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
