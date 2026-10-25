"""Brightness policy (night mode). The device has no clock logic; it applies whatever `bri` it gets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class NightSchedule:
    day_bri: int = 80
    night_bri: int = 10
    night_start: time = time(22, 0)
    night_end: time = time(7, 0)

    def is_night(self, now: datetime) -> bool:
        t = now.time()
        if self.night_start <= self.night_end:
            return self.night_start <= t < self.night_end
        return t >= self.night_start or t < self.night_end  # spans midnight

    def brightness(self, now: datetime) -> int:
        return self.night_bri if self.is_night(now) else self.day_bri
