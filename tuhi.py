#!/usr/bin/env python3
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#

import argparse
import json
import logging
import sys
from gi.repository import GObject

from tuhi.dbusserver import TuhiDBusServer
from tuhi.ble import BlueZDeviceManager
from tuhi.wacom import WacomDevice, Stroke
from tuhi.ble import logger as ble_logger
from tuhi.wacom import logger as wacom_logger
from tuhi.dbusserver import logger as dbusserver_logger

logging.basicConfig(format='%(levelname)s: %(name)s: %(message)s',
                    level=logging.INFO)
logger = logging.getLogger('tuhi')

WACOM_COMPANY_ID = 0x4755


class TuhiDrawing(object):
    class Stroke(object):
        def __init__(self):
            self.points = []

        def to_dict(self):
            d = {}
            d['points'] = [p.to_dict() for p in self.points]
            return d

    class Point(object):
        def __init__(self):
            pass

        def to_dict(self):
            d = {}
            for key in ['toffset', 'position', 'pressure']:
                val = getattr(self, key, None)
                if val is not None:
                    d[key] = val
            return d

    def __init__(self, name, dimensions, timestamp):
        self.name = name
        self.dimensions = dimensions
        self.timestamp = timestamp
        self.strokes = []

    def json(self):
        JSON_FILE_FORMAT_VERSION = 1

        json_data = {
            'version': JSON_FILE_FORMAT_VERSION,
            'devicename': self.name,
            'dimensions': list(self.dimensions),
            'timestamp': self.timestamp,
            'strokes': [s.to_dict() for s in self.strokes]
        }
        return json.dumps(json_data)


class TuhiDevice(GObject.Object):
    """
    Glue object to combine the backend bluez DBus object (that talks to the
    real device) with the frontend DBusServer object that exports the device
    over Tuhi's DBus interface
    """
    def __init__(self, bluez_device, tuhi_dbus_device):
        GObject.Object.__init__(self)
        self._tuhi_dbus_device = tuhi_dbus_device
        self._tuhi_dbus_device.connect('pairing-requested', self._on_pairing)
        self._wacom_device = WacomDevice(bluez_device)
        self._wacom_device.connect('drawing', self._on_drawing_received)
        self._wacom_device.connect('done', self._on_fetching_finished)
        self.drawings = []

        bluez_device.connect('connected', self._on_bluez_device_connected)
        bluez_device.connect('disconnected', self._on_bluez_device_disconnected)
        self._bluez_device = bluez_device
        self.pairing = False

    @property
    def pairingmode(self):
        manufacturer_data = self._bluez_device.get_manufacturer_data(WACOM_COMPANY_ID)

        pairingmode = len(manufacturer_data) == 4

        self._tuhi_dbus_device.pairingmode = pairingmode
        return pairingmode

    def retrieve_data(self):
        self._bluez_device.connect_device()

    def _on_pairing(self, bluez_device):
        logger.debug('{}: pairing requested'.format(self._bluez_device))
        self.pairing = True
        self._bluez_device.connect_device()

    def _on_bluez_device_connected(self, bluez_device):
        logger.debug('{}: connected'.format(bluez_device.address))
        if not self._wacom_device.working:
            if not self.pairing:
                self._wacom_device.start()
            else:
                self._wacom_device.start_pairing()
                self.pairing = False

    def _on_bluez_device_disconnected(self, bluez_device):
        logger.debug('{}: disconnected'.format(bluez_device.address))

    def _on_drawing_received(self, device, drawing):
        logger.debug('Drawing received')
        d = TuhiDrawing(device.name, (0, 0), drawing.timestamp)
        for s in drawing:
            stroke = TuhiDrawing.Stroke()
            lastx, lasty, lastp = None, None, None
            for type, x, y, p in s.points:
                if x is not None:
                    if type == Stroke.RELATIVE:
                        x += lastx
                    lastx = x
                if y is not None:
                    if type == Stroke.RELATIVE:
                        y += lasty
                    lasty = y
                if p is not None:
                    if type == Stroke.RELATIVE:
                        p += lastp
                    lastp = p

                lastx, lasty, lastp = x, y, p
                point = TuhiDrawing.Point()
                point.position = (lastx, lasty)
                point.pressure = lastp
                stroke.points.append(point)
            d.strokes.append(stroke)

        self._tuhi_dbus_device.add_drawing(d)

    def _on_fetching_finished(self, device):
        self._bluez_device.disconnect_device()


class Tuhi(GObject.Object):
    __gsignals__ = {
        "device-added":
            (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "device-connected":
            (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    def __init__(self, debug):
        GObject.Object.__init__(self)
        self.server = TuhiDBusServer()
        self.server.connect('bus-name-acquired', self._on_tuhi_bus_name_acquired)
        self.bluez = BlueZDeviceManager()
        self.bluez.connect('device-updated', self._on_bluez_device_updated)
        self.always_listening = debug

        self.server.start()

        self.devices = {}

    def _on_tuhi_bus_name_acquired(self, dbus_server):
        self.server.bluez = self.bluez

        if self.always_listening:
            self.server._listen()

    def get_tuhi_device(self, bluez_device):
        if bluez_device.address not in self.devices:
            tuhi_dbus_device = self.server.create_device(bluez_device)
            d = TuhiDevice(bluez_device, tuhi_dbus_device)
            self.devices[bluez_device.address] = d

        return self.devices[bluez_device.address]

    def _on_bluez_device_updated(self, manager, bluez_device):
        if bluez_device.vendor_id != WACOM_COMPANY_ID:
            return

        tuhi_dev = self.get_tuhi_device(bluez_device)

        # device is in normal mode, waiting for sync
        if not tuhi_dev.pairingmode:
            tuhi_dev.retrieve_data()


def main(args):
    desc = "Daemon to extract the pen stroke data from Wacom SmartPad devices"
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('-v', '--verbose',
                        help='Show some debugging informations',
                        action='store_true',
                        default=False)
    parser.add_argument('--debug',
                        help='debugging mode, for knowledgeable users only',
                        action='store_true',
                        default=False)

    ns = parser.parse_args(args[1:])
    if ns.verbose or ns.debug:
        for l in [logger, ble_logger, wacom_logger, dbusserver_logger]:
            l.setLevel(logging.DEBUG)

    Tuhi(ns.debug)
    try:
        GObject.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        pass


if __name__ == "__main__":
    main(sys.argv)
