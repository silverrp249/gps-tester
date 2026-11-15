"""Protocol plugins. Importing a module registers its protocol."""
from gps_tester.protocols.base import (Device, Frame, FuzzCase, Position, Protocol,
                                       available_protocols, get_protocol, register)
from gps_tester.protocols import gt06, meitrack, teltonika, tk103  # noqa: F401  (registration)
from gps_tester.protocols.custom import CustomProtocol

__all__ = ["Device", "Frame", "FuzzCase", "Position", "Protocol", "CustomProtocol",
           "available_protocols", "get_protocol", "register"]
