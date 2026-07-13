from typing import Protocol

from .models import InventoryReservation


class InventoryUnavailable(ValueError):
    pass


class InventoryGateway(Protocol):
    def reserve(self, reservation: InventoryReservation) -> None: ...

    def commit(self, reservation: InventoryReservation) -> None: ...

    def release(self, reservation: InventoryReservation) -> None: ...
