""" MeshCore connect helpers, shared server/client.
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
from meshcore import EventType, MeshCore
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

async def _bluez_known_device(address: str):
    # Return a bleak BLEDevice for an already-bonded address, or None.
    try:
        from bleak.backends.device import BLEDevice
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError:
        return None

    suffix = "dev_" + address.upper().replace(":", "_")
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:
        return None
    try:
        introspection = await bus.introspect("org.bluez", "/")
        obj = bus.get_proxy_object("org.bluez", "/", introspection)
        manager = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        for path, interfaces in (await manager.call_get_managed_objects()).items():
            if not path.endswith(suffix):
                continue
            props = interfaces.get("org.bluez.Device1")
            if props is None:
                continue
            plain = {k: v.value for k, v in props.items()}
            logger.debug("using BlueZ device object %s for %s", path, address)
            return BLEDevice(address, plain.get("Name"), {"path": path, "props": plain})
    except Exception as exc:
        logger.debug("BlueZ device lookup for %s failed: %s", address, exc)
    finally:
        bus.disconnect()
    return None

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
    # BLEConnection that clears a stale BlueZ link on its first connect not on retry.
    def __init__(self, *args, force_disconnect_before_first_connect: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending_force_disconnect = force_disconnect_before_first_connect

    async def connect(self):
        if self._pending_force_disconnect and isinstance(self.address, str):
            await _bluez_force_disconnect(self.address)
        self._pending_force_disconnect = False
        if self.device is None and isinstance(self.address, str):
            # BLEConnection.connect() uses `device` verbatim when set
            self.device = await _bluez_known_device(self.address)
        return await super().connect()

# USB-serial bridges example list for easy auto discovery 
_KNOWN_BRIDGES = {
    (0x10C4, 0xEA60),  # CP210x: Heltec, LilyGO
    (0x1A86, 0x7523),  # WCH CH340
    (0x1A86, 0x55D4),  # WCH CH9102
    (0x239A, None),    # Adafruit USB CDC
    (0x303A, None),    # Espressif native USB
}

def candidate_serial_ports() -> list[str]:
    # Serial ports that might have a radio on them
    from serial.tools import list_ports

    def rank(port) -> tuple:
        known = (port.vid, port.pid) in _KNOWN_BRIDGES or (port.vid, None) in _KNOWN_BRIDGES
        return (0 if known else 1, port.device)

    # USB only. A PC advertises ~32 legacy /dev/ttyS* ports
    usb = [p for p in list_ports.comports() if p.vid is not None]
    return [p.device for p in sorted(usb, key=rank)]

async def autodetect_serial(baudrate: int = 115200) -> str | None:
    # Work out which port the radio is on by asking each one.
    for port in candidate_serial_ports():
        logger.info("probing %s for a MeshCore radio", port)
        try:
            mc = await connect_serial(port, baudrate, auto_reconnect=False, quiet=True)
        except Exception as exc:
            logger.debug("%s did not answer: %s", port, exc)
            continue
        if mc is None:
            continue
        try:
            result = await mc.commands.send_device_query()
            if result is not None and result.type != EventType.ERROR:
                logger.info("found a MeshCore radio on %s", port)
                return port
        except Exception as exc:
            logger.debug("%s answered but not as MeshCore: %s", port, exc)
        finally:
            await _close_quietly(mc)
    logger.warning("no MeshCore radio found on any serial port")
    return None

async def _close_quietly(mc: MeshCore) -> None:
    # Failed connect/cleanup 
    try:
        await mc.disconnect()
    except Exception as exc:
        logger.debug("cleanup disconnect failed: %s", exc)

async def connect_serial(
    port: str,
    baudrate: int = 115200,
    *,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
    quiet: bool = False,
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
        only_error=quiet,
    )
    try:
        result = await mc.connect()
    except Exception:
        # A raised connect leaves the tty open
        await _close_quietly(mc)
        raise
    if result is None:
        await _close_quietly(mc)
        return None
    return mc

async def connect_ble(
    address: str,
    pin: str | None = None,
    *,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
    quiet: bool = False,
    force_disconnect_before_first_connect: bool = True,) -> MeshCore | None:
    #Mirrors MeshCore.create_ble(), but with autoconnect
    connection = _ResilientBLEConnection(
        address=address,
        pin=pin,
        force_disconnect_before_first_connect=force_disconnect_before_first_connect,
    )
    mc = MeshCore(
        connection,
        auto_reconnect=auto_reconnect,
        max_reconnect_attempts=max_reconnect_attempts,
        only_error=quiet,
    )
    try:
        result = await mc.connect()
    except Exception:
        # Same leak as the serial path
        await _close_quietly(mc)
        raise
    if result is None:
        await _close_quietly(mc)
        return None
    return mc
