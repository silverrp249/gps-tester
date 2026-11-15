"""Data generators: realistic routes and malformed inputs."""
from gps_tester.generators.fuzz import build_cases
from gps_tester.generators.route import RouteSimulator, make_imei

__all__ = ["RouteSimulator", "make_imei", "build_cases"]
