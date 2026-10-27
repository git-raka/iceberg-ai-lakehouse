"""Electric shuttle bus simulator: publishes telemetry to Kafka (topic ebus.telemetry) every few seconds.

Six buses loop around a made-up estate road network. About 1% of messages are deliberately broken
(impossible battery level or coordinates) so the Silver layer's validation has work to do.
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timezone

from confluent_kafka import Producer

from ..settings import settings

TOPIC = "ebus.telemetry"
CENTER = (0.0100, 0.0100)  # next to "Null Island" (0, 0): deliberately no real place, the estate is fictional
ROUTES = {"R1": 0.010, "R2": 0.016, "R3": 0.022}


def run(interval_s: float = 2.0, buses: int = 6, iterations: int | None = None) -> None:
    producer = Producer({"bootstrap.servers": settings().kafka_bootstrap, "linger.ms": 50})
    r = random.Random()
    state = {f"BUS-{i + 1:02d}": {"route": list(ROUTES)[i % 3], "angle": r.uniform(0, 2 * math.pi),
                                   "battery": r.uniform(60, 100), "passengers": r.randint(0, 20)}
             for i in range(buses)}
    n = 0
    while iterations is None or n < iterations:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for bus_id, s in state.items():
            radius = ROUTES[s["route"]]
            speed = max(0.0, r.gauss(28, 8))
            s["angle"] += speed / 3600 * interval_s / (radius * 111) * 2
            s["battery"] = s["battery"] - r.uniform(0.01, 0.05) if s["battery"] > 15 else 100.0  # recharged
            s["passengers"] = max(0, min(40, s["passengers"] + r.randint(-2, 2)))
            msg = {"bus_id": bus_id, "route_id": s["route"], "ts": now,
                   "lat": round(CENTER[0] + radius * math.sin(s["angle"]), 6),
                   "lon": round(CENTER[1] + radius * math.cos(s["angle"]), 6),
                   "speed_kmh": round(speed, 1), "battery_pct": round(s["battery"], 1), "passengers": s["passengers"]}
            if r.random() < 0.01:
                msg["battery_pct"] = 140.0 if r.random() < 0.5 else msg["battery_pct"]
                msg["lat"] = 999.0 if msg["battery_pct"] != 140.0 else msg["lat"]
            producer.produce(TOPIC, key=bus_id, value=json.dumps(msg))
        producer.poll(0)
        producer.flush(5)
        n += 1
        time.sleep(interval_s)


if __name__ == "__main__":
    run()
