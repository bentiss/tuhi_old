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

import logging
import sys
from enum import Enum
from gi.repository import GObject, GLib, Gio

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('ble')

ORG_BLUEZ_GATTCHARACTERISTIC1 = 'org.bluez.GattCharacteristic1'
ORG_BLUEZ_GATTSERVICE1 = 'org.bluez.GattService1'
ORG_BLUEZ_DEVICE1 = 'org.bluez.Device1'
ORG_BLUEZ_ADAPTER1 = 'org.bluez.Adapter1'

class BlueZCharacteristic(GObject.Object):
    """
    Abstraction for a org.bluez.GattCharacteristic1 object

    :param obj: the org.bluez.GattCharacteristic1 DBus proxy object
    """
    def __init__(self, obj):
        self.obj = obj
        self.objpath = obj.get_object_path()
        self.interface = obj.get_interface(ORG_BLUEZ_GATTCHARACTERISTIC1)
        assert(self.interface is not None)

        self.uuid = self.interface.get_cached_property('UUID').unpack()
        assert(self.uuid is not None)

        self._property_callbacks = {}
        self.interface.connect('g-properties-changed',
                               self._on_properties_changed)

    def connect_property(self, propname, callback):
        """
        Connect the property with the given name to the callback function
        provide. When the property chages, callback is invoked as:

            callback(propname, value)
        """
        self._property_callbacks[propname] = callback

    def start_notify(self):
        self.interface.StartNotify()

    def write_value(self, data):
        return self.interface.WriteValue('(aya{sv})', data, {})

    def _on_properties_changed(self, obj, properties, invalidated_properties):
        properties = properties.unpack()
        for name, value in properties.items():
            try:
                self._property_callbacks[name](name, value)
            except KeyError:
                pass

    def __repr__(self):
        return 'Characteristic {}:{}'.format(self.uuid, self.objpath)


class BlueZDevice(GObject.Object):
    """
    Abstraction for a org.bluez.Device1 object

    The device initializes itself based on the given object manager and
    object, specifically: it resolves its services an gatt characteristics.

    :param om: The ObjectManager for name org.bluez path /
    :param obj: The org.bluez.Device1 DBus proxy object
    
    """
    __gsignals__ = {
            "connected":
                (GObject.SIGNAL_RUN_FIRST, None, ()),
            "disconnected":
                (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    def __init__(self, om, obj):
        GObject.Object.__init__(self)
        self.objpath = obj.get_object_path()
        self.obj = obj
        self.interface = obj.get_interface(ORG_BLUEZ_DEVICE1)
        assert(self.interface is not None)

        self.address = self.interface.get_cached_property('Address').get_string()
        self.name = self.interface.get_cached_property('Name').get_string()
        self.uuids = self.interface.get_cached_property('UUIDs')
        self.vendor_id = 0
        md = self.interface.get_cached_property('ManufacturerData')
        if md is not None:
            self.vendor_id = md.keys()[0]

        assert(self.name is not None)
        assert(self.address is not None)
        assert(self.uuids is not None)
        logger.debug('Device {} - {} - {}'.format(self.objpath, self.address, self.name))

        self.characteristics = {}
        self.resolve(om)
        self.interface.connect('g-properties-changed', self._on_properties_changed)
        if self.interface.get_cached_property('Connected').get_boolean():
            self.emit('connected')

    def resolve(self, om):
        """
        Resolve the GattServices and GattCharacteristics. This function does
        not need to be called for existing objects but if a device comes in
        at runtime not all services may have been resolved by the time the
        org.bluez.Device1 shows up.
        """
        objects = om.get_objects()
        self._resolve_gatt_services(objects)

    def _resolve_gatt_services(self, objects):
        self.gatt_services = []
        for obj in objects:
            i = obj.get_interface(ORG_BLUEZ_GATTSERVICE1)
            if i is None:
                continue

            device = i.get_cached_property('Device').get_string()
            if device != self.objpath:
                continue

            logger.debug("GattService1: {} for device {}".format(obj.get_object_path(), device))
            self.gatt_services.append(obj)
            self._resolve_gatt_characteristics(obj, objects)

    def _resolve_gatt_characteristics(self, service_obj, objects):
        for obj in objects:
            i = obj.get_interface(ORG_BLUEZ_GATTCHARACTERISTIC1)
            if i is None:
                continue

            service = i.get_cached_property('Service').get_string()
            if service != service_obj.get_object_path():
                continue

            chrc = BlueZCharacteristic(obj)
            if chrc.uuid in self.characteristics:
                continue

            logger.debug("GattCharacteristic: {} for service {}".format(chrc.uuid, service))

            self.characteristics[chrc.uuid] = chrc

    def connect_device(self):
        """
        Connect to the bluetooth device via bluez. This function is
        asynchronous and returns immediately.
        """
        i = self.obj.get_interface(ORG_BLUEZ_DEVICE1)
        if i.get_cached_property('Connected').get_boolean():
            logger.info('{}: Device is already connected'.format(self.address))
            self.emit('connected')
            return

        logger.info('{}: Connecting'.format(self.address))
        i.Connect(result_handler=self._on_connect_result)

    def _on_connect_result(self, obj, result, user_data):
        if isinstance(result, Exception):
            logger.error('Connection failed: {}'.format(result))

    def _on_properties_changed(self, obj, properties, invalidated_properties):
        properties = properties.unpack()

        if 'Connected' in properties:
            if properties['Connected']:
                logger.info('Connection established')
                self.emit('connected')
            else:
                logger.info('Disconnected')
                self.emit('disconnected', self)

    def connect_gatt_value(self, uuid, callback):
        """
        Connects Value property changes of the given GATT Characteristics
        UUID to the callback.
        """
        try:
            chrc = self.characteristics[uuid]
            chrc.connect_property('Value', callback)
            chrc.start_notify()
        except KeyError:
            pass

    # this is wacom-specific, not BlueZDevice specific, needs to be moved
    # out somehow
    def _start_notifications(self):
        self._start_gatt_notification(WACOM_CHRC_LIVE_PEN_DATA_UUID,
                                     self._pen_data_changed_cb)
        self._start_gatt_notification(WACOM_OFFLINE_CHRC_PEN_DATA_UUID,
                                     self._pen_data_received_cb)
        self._start_gatt_notification(NORDIC_UART_CHRC_RX_UUID,
                                     self._receive_nordic_data_cb)

    def _start_gatt_notification(self, uuid, callback):
        try:
            chrc = self.characteristics[uuid]
            chrc.connect_properties(callback)
        except KeyError:
            pass

    def _pen_data_changed_cb(self, obc, changed_props, invalidated_props):
        print('pen data changed')

    def _pen_data_received_cb(self, obc, changed_props, invalidated_props):
        print('pen data received')

    def _receive_nordic_data_cb(self, obc, changed_props, invalidated_props):
        print('nordic data received')

    def start(self):
        self.retrieve_data()

    def __repr__(self):
        return 'Device {}:{}'.format(self.name, self.objpath)

class BlueZDeviceManager(GObject.Object):
    """
    Manager object that connects to org.bluez's root object and handles the
    devices. If device_filter_callback is set, it is called for each device
    and expected to return True if the device should be used or False if the
    device should be ignored.
    """
    __gsignals__ = {
            "device-added":
                (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    def __init__(self, **kwargs):
        GObject.Object.__init__(self, **kwargs)
        self.devices = []

    def connect_to_bluez(self):
        self._om = Gio.DBusObjectManagerClient.new_for_bus_sync(
                    Gio.BusType.SYSTEM,
                    Gio.DBusObjectManagerClientFlags.NONE,
                    'org.bluez',
                    '/',
                    None,
                    None,
                    None)
        self._om.connect('object-added', self._on_om_object_added)
        self._om.connect('object-removed', self._on_om_object_removed)

        # We rely on nested object paths, so let's sort the objects by
        # object path length and process them in order, this way we're
        # guaranteed that the objects we need already exist.
        for obj in self._om.get_objects(): 
            self._process_object(obj)

    def _on_om_object_added(self, om, obj):
        """Callback for ObjectManager's object-added"""
        objpath = obj.get_object_path()
        logger.debug('Object added: {}'.format(objpath))
        needs_resolve = self._process_object(obj)

        # we had at least one characteristic added, need to resolve the 
        # devices.
        # FIXME: this isn't the most efficient way...
        if needs_resolve:
            for d in self.devices:
                d.resolve(om)

    def _on_om_object_removed(self, om, obj):
        """Callback for ObjectManager's object-removed"""
        objpath = obj.get_object_path()
        logger.debug('Object removed: {}'.format(objpath))

    def _process_object(self, obj):
        """Process a single DBusProxyObject"""

        if obj.get_interface(ORG_BLUEZ_ADAPTER1) is not None:
            self._process_adapter(obj)
        elif obj.get_interface(ORG_BLUEZ_DEVICE1) is not None:
            self._process_device(obj)
        elif obj.get_interface(ORG_BLUEZ_GATTCHARACTERISTIC1) is not None:
            return True

        return False

    def _process_adapter(self, obj):
        objpath = obj.get_object_path()
        logger.debug('Adapter: {}'.format(objpath))
        # FIXME: call StartDiscovery if we want to pair

    def _process_device(self, obj):
        objpath = obj.get_object_path()
        dev = BlueZDevice(self._om, obj)
        self.devices.append(dev)
        self.emit("device-added", dev)

    def _process_characteristic(self, obj):
        objpath = obj.get_object_path()
        logger.debug('Characteristic {}'.format(objpath))

