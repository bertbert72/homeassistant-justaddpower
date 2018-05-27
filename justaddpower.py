"""
Support for the Just Add Power 2G HP over IP system (Cisco)

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/justaddpower
"""
import logging
import socket
import time
import re

import voluptuous as vol

from homeassistant.components.media_player import (
    DOMAIN, MEDIA_PLAYER_SCHEMA, PLATFORM_SCHEMA, SUPPORT_SELECT_SOURCE,
    MediaPlayerDevice)
from homeassistant.const import (
    ATTR_ENTITY_ID, CONF_NAME, CONF_HOST, CONF_PORT, STATE_OFF, STATE_ON,
    CONF_USERNAME,CONF_PASSWORD)
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

SUPPORT_JUSTADDPOWER = SUPPORT_SELECT_SOURCE

ZONE_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
})

SOURCE_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
})

CONF_ZONES = 'zones'
CONF_SOURCES = 'sources'
CONF_RXSUBNET = 'rxsubnet'

DATA_JUSTADDPOWER = 'justaddpower'


# Valid zone ids: 1-99
ZONE_IDS = vol.All(vol.Coerce(int), vol.Range(min=1, max=99))

# Valid source ids: 1-99
SOURCE_IDS = vol.All(vol.Coerce(int), vol.Range(min=1, max=99))

PLATFORM_SCHEMA = vol.All(
    PLATFORM_SCHEMA.extend({
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_ZONES): vol.Schema({ZONE_IDS: ZONE_SCHEMA}),
        vol.Required(CONF_SOURCES): vol.Schema({SOURCE_IDS: SOURCE_SCHEMA}),
        vol.Optional(CONF_USERNAME, default="cisco"): cv.string,
        vol.Optional(CONF_PASSWORD, default="cisco"): cv.string,
        vol.Optional(CONF_RXSUBNET, default="10.128.0"): cv.string,
    }))


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Just Add Power platform."""
    if DATA_JUSTADDPOWER not in hass.data:
        hass.data[DATA_JUSTADDPOWER] = {}

    host = config.get(CONF_HOST)
    swUser = config.get(CONF_USERNAME)
    swPassword = config.get(CONF_PASSWORD)
    rxsubnet = config.get(CONF_RXSUBNET)

    sources = {source_id: extra[CONF_NAME] for source_id, extra
               in config[CONF_SOURCES].items()}

    devices = []
    for zone_id, extra in config[CONF_ZONES].items():
        _LOGGER.info("Adding zone %d - %s", zone_id, extra[CONF_NAME])
        unique_id = "{}-{}".format(host, zone_id)
        device = JustaddpowerZone(host, swUser, swPassword, rxsubnet, sources, zone_id, extra[CONF_NAME])
        hass.data[DATA_JUSTADDPOWER][unique_id] = device
        devices.append(device)

    add_devices(devices, True)


class JustaddpowerZone(MediaPlayerDevice):
    """Representation of a Just Add Power zone."""

    def __init__(self, host, swUser, swPassword, rxsubnet, sources, zone_id, zone_name):
        """Initialize new zone."""
        self._host = host
        self._user = swUser
        self._password = swPassword
        self._source_id_name = sources
        self._source_name_id = {v: k for k, v in sources.items()}
        # ordered list of all source names
        self._source_names = sorted(self._source_name_id.keys(),
                                    key=lambda v: self._source_name_id[v])
        self._zone_id = zone_id
        self._name = zone_name
        #self._state = STATE_ON
        self._source = None
        self._debug = False
        self._sock = None
        self._port = 23
        self._rxsubnet = rxsubnet

        bufsize=1024
        try:
            self.connect(self._host, self._port)
            tosend = self._user + "\r" + self._password + "\r show vlan\r"
            if self._debug:
                _LOGGER.warning("Sending request: '%s'", tosend)
            try:
                self._sock.sendall(tosend.encode())
                time.sleep(1)
                data = self._sock.recv(bufsize)
                if self._debug:
                    _LOGGER.warning("Received response: '%s'", data)
                japConfig = re.search('(?<=JAP_)\d+x\d+', data[50:-1].decode()).group(0)
                self._txCount = int(re.search('\d+',japConfig).group(0))
                self._rxCount = int(re.search('(?<=x)\d+',japConfig).group(0))
                _LOGGER.info("Configured for Tx: "+str(self._txCount)+", Rx: "+str(self._rxCount))
            except socket.timeout:
                _LOGGER.warning("Connection timed out...")
            self.disconnect()
        except Exception:
            raise

    def update(self):
        """Retrieve latest state."""
        bufsize=1024
        idx = 0
        try:
            self.connect(self._host, self._port)
            rxPort = self._zone_id + self._txCount + 1
            tosend = self._user + "\r" + self._password + "\r show interface switchport ge " + str(rxPort) + "\r"
            if self._debug:
                _LOGGER.warning("Sending request: '%s'", tosend)
            try:
                self._sock.sendall(tosend.encode())
                time.sleep(1)
                data = self._sock.recv(bufsize)
                if self._debug:
                    _LOGGER.warning("Received response: '%s'", data)
                idx = int(re.search('(?<=TRANSMITTER_)\d+', data[50:-1].decode()).group(0))
            except socket.timeout:
                _LOGGER.warning("Connection timed out...")
            self.disconnect()
        except Exception:
            raise

        if idx in self._source_id_name:
            self._source = self._source_id_name[idx]
        else:
            self._source = None

    def connect(self, host, port):
        try:
            if self._debug:
                _LOGGER.warning("Connecting to device...")
            self._sock = socket.socket()
            self._sock.settimeout(5)
            self._sock.connect((host, port))
        except Exception:
            raise

    def disconnect(self):
        if self._debug:
            _LOGGER.warning("Disconnecting from device...")
        self._sock.close()

    @property
    def name(self):
        """Return the name of the zone."""
        return self._name

    @property
    def supported_features(self):
        """Return flag of media commands that are supported."""
        return SUPPORT_JUSTADDPOWER

    @property
    def media_title(self):
        """Return the current source as media title."""
        return self._source

    @property
    def source(self):
        """Return the current input source of the device."""
        return self._source

    @property
    def source_list(self):
        """List of available input sources."""
        return self._source_names

    def select_source(self, source):
        """Set input source."""
        if source not in self._source_name_id:
            return
        idx = self._source_name_id[source]
        _LOGGER.info("Setting zone %d source to %s", self._zone_id, idx)

        bufsize=1024
        try:
            self.connect(self._host, self._port)
            rxPort = self._zone_id + self._txCount + 1
            txPort = idx + 10
            tosend = self._user + "\r" + self._password + "\rconf\r int ge" + str(rxPort) + "\r sw g al v r 11-399\r sw g al v a " + str(txPort) + " u\r end\r"

            if self._debug:
                _LOGGER.warning("Sending request: '%s'", tosend)

            try:
                self._sock.sendall(tosend.encode())
                time.sleep(0.3)
                data = self._sock.recv(bufsize)
                if self._debug:
                    _LOGGER.warning("Received response: '%s'", data)
            except socket.timeout:
                _LOGGER.warning("Connection timed out...")

            self.disconnect()

            time.sleep(1)
            _LOGGER.debug("Connecting to: " + self._rxsubnet + "." + str(self._zone_id+1))
            self.connect(self._rxsubnet + '.' + str(self._zone_id+1), self._port)
            tosend = "e e_reconnect\r"

            if self._debug:
                _LOGGER.warning("Sending request: '%s'", tosend)

            try:
                self._sock.sendall(tosend.encode())
                time.sleep(0.3)
                data = self._sock.recv(bufsize)
                if self._debug:
                    _LOGGER.warning("Received response: '%s'", data)
            except socket.timeout:
                _LOGGER.warning("Connection timed out...")

            self.disconnect()

            return
        except Exception:
            raise
