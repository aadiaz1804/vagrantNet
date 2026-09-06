""" MeshCore connect helpers, shared daemon/client.
1. Hardware autoreconnect
   - serial: this radio's companion session won't complete CMD_APP_START
     unless DTR/RTS is pulsed after the first connect, since a totally fresh session doesn't need it.
   - BLE: unblock link from a previous session (see `_bluez_force_disconnect`).
2. `auto_reconnect=True` on the `MeshCore` instance itself
   detects an unexpected disconnect and retries the transport connect + CMD_APP_START 
   up to `max_reconnect_attempts`.
"""

from __future__ import annotations

import asyncio
import logging
import serial
from meshcore import MeshCore
from meshcore.ble_cx import BLEConnection
from meshcore.serial_cx import SerialConnection

logger = logging.getLogger("vagrantnet.transport")

DEFAULT_MAX_RECONNECT_ATTEMPTS = 8

async def _pulse_serial_dtr(port: str) -> None:
    # Force-drop DTR/RTS on the companion radio's serial port.
    try:
        line = serial.Serial(port)
        line.dtr = True
        line.rts = True
        await asyncio.sleep(0.3)
        line.dtr = False
        line.rts = False
        await asyncio.sleep(1.0)
        line.close()
        await asyncio.sleep(2.0)
    except serial.SerialException as exc:
        logger.warning("could not reset serial session on %s: %s", port, exc)

async def _bluez_force_disconnect(address: str) -> None:
    # Best-effort: drop any existing BlueZ-level link to `address` first.
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError:
        return

    dev_suffix = "dev_" + address.upper().replace(":", "_")
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:
        return
    try:
        for adapter in ("hci0", "hci1"):
            path = f"/org/bluez/{adapter}/{dev_suffix}"
            try:
                introspection = await bus.introspect("org.bluez", path)
                obj = bus.get_proxy_object("org.bluez", path, introspection)
                device = obj.get_interface("org.bluez.Device1")
                if not await device.get_connected():
                    continue
                await device.call_disconnect()
            except Exception:
                continue  # not on this adapter, or already disconnected
    finally:
        bus.disconnect()

class _ResilientSerialConnection(SerialConnection):
    # SerialConnection that pulses DTR/RTS before every connect after the first

    def __init__(self, *args, pulse_before_first_connect: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected_once = pulse_before_first_connect

    async def connect(self):
        if self._connected_once:
            await _pulse_serial_dtr(self.port)
        result = await super().connect()
        if result is not None:
            self._connected_once = True
        return result

class _ResilientBLEConnection(BLEConnection):
    # BLEConnection that clears any stale BlueZ link before every connect

    async def connect(self):
        if self.address:
            await _bluez_force_disconnect(self.address)
        return await super().connect()

async def connect_serial(
    port: str,
    baudrate: int = 115200,
    *,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
    pulse_before_first_connect: bool = False,
) -> MeshCore | None:
    """Mirrors MeshCore.create_serial(), but with the hygiene-wrapped
    connection and auto_reconnect wired in from the start."""
    connection = _ResilientSerialConnection(
        port, baudrate, pulse_before_first_connect=pulse_before_first_connect
    )
    mc = MeshCore(
        connection,
        auto_reconnect=auto_reconnect,
        max_reconnect_attempts=max_reconnect_attempts,
    )
    result = await mc.connect()
    if result is None:
        await mc.disconnect()
        return None
    return mc


async def connect_ble(
    address: str,
    pin: str | None = None,
    *,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
) -> MeshCore | None:
    """Mirrors MeshCore.create_ble(), but with the hygiene-wrapped
    connection and auto_reconnect wired in from the start."""
    connection = _ResilientBLEConnection(address=address, pin=pin)
    mc = MeshCore(
        connection,
        auto_reconnect=auto_reconnect,
        max_reconnect_attempts=max_reconnect_attempts,
    )
    result = await mc.connect()
    if result is None:
        await mc.disconnect()
        return None
    return mc
