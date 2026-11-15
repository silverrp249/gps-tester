"""Virtual vehicles driving a smooth random route around a centre point."""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

from gps_tester.protocols.base import Position

METERS_PER_DEGREE = 111_320.0


def make_imei(base: str, index: int) -> str:
    """Build a valid 15-digit IMEI (Luhn check digit) from a 14-digit base + index."""
    body = str(int(base[:14].ljust(14, "0")) + index).rjust(14, "0")[-14:]
    total = 0
    for i, ch in enumerate(body):
        digit = int(ch)
        if i % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return body + str((10 - total % 10) % 10)


class RouteSimulator:
    """Stateful generator: every :meth:`step` moves the vehicle forward in time."""

    def __init__(self, lat: float, lon: float, rng: random.Random, radius_km: float = 15.0) -> None:
        self.rng = rng
        self.center = (lat, lon)
        self.radius_m = radius_km * 1000
        distance = rng.uniform(0, self.radius_m * 0.8)
        bearing = rng.uniform(0, 2 * math.pi)
        self.lat = lat + distance * math.cos(bearing) / METERS_PER_DEGREE
        self.lon = lon + distance * math.sin(bearing) / self._lon_scale(lat)
        self.course = rng.uniform(0, 360)
        self.speed = rng.uniform(20, 90)          # km/h
        self.altitude = rng.uniform(550, 650)
        self.odometer = rng.uniform(1_000, 150_000) * 1000
        self._stopped_for = 0.0

    @staticmethod
    def _lon_scale(lat: float) -> float:
        return METERS_PER_DEGREE * max(0.01, math.cos(math.radians(lat)))

    def step(self, dt: float) -> Position:
        """Advance ``dt`` seconds and return the new fix."""
        rng = self.rng
        if self._stopped_for > 0:                 # waiting at a traffic light / parked
            self._stopped_for -= dt
            self.speed = 0.0
        else:
            self.speed = min(130.0, max(5.0, self.speed + rng.uniform(-8, 8)))
            if rng.random() < 0.03:
                self._stopped_for = rng.uniform(5, 60)
            self.course = (self.course + rng.uniform(-20, 20)) % 360

        d_lat = (self.lat - self.center[0]) * METERS_PER_DEGREE
        d_lon = (self.lon - self.center[1]) * self._lon_scale(self.lat)
        if math.hypot(d_lat, d_lon) > self.radius_m:   # steer back toward the centre
            self.course = math.degrees(math.atan2(-d_lon, -d_lat)) % 360

        meters = self.speed / 3.6 * dt
        heading = math.radians(self.course)
        self.lat += meters * math.cos(heading) / METERS_PER_DEGREE
        self.lon += meters * math.sin(heading) / self._lon_scale(self.lat)
        self.odometer += meters
        self.altitude = min(900.0, max(400.0, self.altitude + rng.uniform(-2, 2)))
        return Position(
            lat=round(self.lat, 6), lon=round(self.lon, 6), speed=round(self.speed, 1),
            course=round(self.course, 1), altitude=round(self.altitude, 1),
            satellites=rng.randint(7, 14), timestamp=datetime.now(timezone.utc),
            ignition=True, odometer=self.odometer,
        )
