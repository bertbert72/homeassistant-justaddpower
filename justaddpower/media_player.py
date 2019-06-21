"""
Support for the Just Add Power HD over IP system (Cisco/Luxul)

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/justaddpower
"""
import logging
import socket
import threading
import time
import re
import queue
import types
import ipaddress
import random

import voluptuous as vol

from homeassistant.components.media_player import (
    DOMAIN, PLATFORM_SCHEMA, SUPPORT_SELECT_SOURCE, MediaPlayerDevice)
from homeassistant.const import (
    CONF_NAME, CONF_HOST, CONF_URL, STATE_OFF, STATE_ON, CONF_USERNAME, CONF_PASSWORD, CONF_IP_ADDRESS, CONF_TYPE)
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

SUPPORT_JUSTADDPOWER = SUPPORT_SELECT_SOURCE

CONF_SWITCH = 'switch'
CONF_RECEIVERS = 'receivers'
CONF_TRANSMITTERS = 'transmitters'
CONF_RX_SUBNET = 'rx_subnet'
CONF_MIN_REFRESH_INTERVAL = 'min_refresh_interval'
CONF_USB = 'usb'
CONF_IMAGE_PULL = 'image_pull'
CONF_IMAGE_PULL_REFRESH = 'image_pull_refresh'

DATA_JUSTADDPOWER = 'justaddpower'
TELNET_PORT = 23

SWITCH_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_USERNAME, default="cisco"): cv.string,
    vol.Optional(CONF_PASSWORD, default="cisco"): cv.string,
    vol.Optional(CONF_RX_SUBNET, default="10.128.0.0"): cv.string,
    vol.Optional(CONF_MIN_REFRESH_INTERVAL, default=10): cv.positive_int,
    vol.Optional(CONF_TYPE, default="cisco"): cv.string,
})

RECEIVER_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Optional(CONF_USB, default=False): cv.boolean,
    vol.Optional(CONF_IP_ADDRESS, default=""): cv.string,
    vol.Optional(CONF_IMAGE_PULL, default=False): cv.boolean,
    vol.Optional(CONF_IMAGE_PULL_REFRESH, default=10): cv.positive_int,
})

TRANSMITTER_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Optional(CONF_USB, default=False): cv.boolean,
    vol.Optional(CONF_URL, default=""): cv.string,
})

# Valid receiver ids: 1-350
RECEIVERS_IDS = vol.All(vol.Coerce(int), vol.Range(min=1, max=350))

# Valid transmitter ids: 1-350
TRANSMITTER_IDS = vol.All(vol.Coerce(int), vol.Range(min=1, max=350))

PLATFORM_SCHEMA = vol.All(
    PLATFORM_SCHEMA.extend({
        vol.Required(CONF_SWITCH): vol.Schema(SWITCH_SCHEMA),
        vol.Required(CONF_RECEIVERS): vol.Schema({RECEIVERS_IDS: RECEIVER_SCHEMA}),
        vol.Required(CONF_TRANSMITTERS): vol.Schema({TRANSMITTER_IDS: TRANSMITTER_SCHEMA}),
    }))


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Just Add Power platform."""
    if DATA_JUSTADDPOWER not in hass.data:
        hass.data[DATA_JUSTADDPOWER] = {}

    rx_gateway = int(ipaddress.ip_address(config[CONF_SWITCH].get(CONF_RX_SUBNET))) + 1
    switch = types.SimpleNamespace()
    switch.host = config[CONF_SWITCH].get(CONF_HOST)
    switch.user = config[CONF_SWITCH].get(CONF_USERNAME)
    switch.password = config[CONF_SWITCH].get(CONF_PASSWORD)
    switch.min_refresh_interval = config[CONF_SWITCH].get(CONF_MIN_REFRESH_INTERVAL)
    switch.sock = None
    switch.queue = queue.Queue(1)
    switch.last_refresh = 0
    switch.last_response = ""
    switch.tx_count = 0
    switch.rx_count = 0
    switch.type = config[CONF_SWITCH].get(CONF_TYPE).lower()

    transmitters = {}
    for transmitter_id, extra in config[CONF_TRANSMITTERS].items():
        transmitter_info = {
            CONF_NAME: extra[CONF_NAME],
            CONF_USB: extra[CONF_USB],
            CONF_URL: extra[CONF_URL],
        }
        transmitters[transmitter_id] = transmitter_info

    devices = []
    for receiver_id, extra in config[CONF_RECEIVERS].items():
        _LOGGER.info("Adding Rx%d - %s", receiver_id, extra[CONF_NAME])
        unique_id = "{}-{}".format(switch.host, receiver_id)
        rx_ip = extra[CONF_IP_ADDRESS] or (ipaddress.ip_address(rx_gateway + receiver_id).__str__())
        device = JustaddpowerReceiver(switch, rx_ip, extra[CONF_IMAGE_PULL], extra[CONF_IMAGE_PULL_REFRESH],
                                      transmitters, receiver_id, extra[CONF_NAME])
        hass.data[DATA_JUSTADDPOWER][unique_id] = device
        devices.append(device)

    add_devices(devices, True)


class JustaddpowerReceiver(MediaPlayerDevice):
    """Representation of a Just Add Power receiver."""

    def __init__(self, switch, rx_ip, image_pull, image_pull_refresh, transmitters, receiver_id, receiver_name):
        """Initialize new receiver."""

        self._transmitter = None
        self._transmitters = transmitters
        self._transmitter_name_id = {v[CONF_NAME]: k for k, v in transmitters.items()}
        self._transmitter_names = sorted(self._transmitter_name_id.keys(), key=lambda v: self._transmitter_name_id[v])
        self._receiver_id = receiver_id
        self._receiver_name = receiver_name
        self._state = STATE_OFF
        self._trace = False
        self._rx_sock = None
        self._rx_ip = rx_ip
        self._rx_mac = None
        self._image_pull = image_pull
        self._image_pull_refresh = image_pull_refresh
        self._image_pull_last_refresh = 0
        self._image_pull_id = 0
        self._switch = switch

        self.get_switch_config()

        try:
            cmd = "ifconfig | grep eth0:stat\r"
            try:
                data = self.rx_cmd(cmd)
                _LOGGER.debug("Rx%d: received response [%s]", self._receiver_id, data)
                self._rx_mac = re.search('([0-9A-F]{2}[:-]){5}([0-9A-F]{2})', data.decode()).group(0)
                _LOGGER.info("Rx%d: receiver MAC [%s]", self._receiver_id, self._rx_mac)
                self._state = STATE_ON
            except socket.timeout:
                _LOGGER.warning("Rx%d: connection timed out", self._receiver_id)
        except Exception:
            raise

    def decode_vlan_cisco(self, splits):
        run_on_line = False
        tx_vals = ""
        tx_id = ""
        rx_list = {}
        for line in splits:
            split_data = line.split()
            if not run_on_line and len(split_data) >= 3 and "TRANSMITTER_" in split_data[1]:
                if "gi" in split_data[2]:
                    tx_id = split_data[1][12:]
                    tx_vals = split_data[2].replace("gi", "")
                    if tx_vals[-1:] == ",":
                        run_on_line = True
                    else:
                        tx_vals = self.expand_range(tx_vals)
            elif run_on_line:
                tx_vals = tx_vals + split_data[0].replace("gi", "")
                if tx_vals[-1:] != ",":
                    run_on_line = False
                    tx_vals = self.expand_range(tx_vals)
            else:
                tx_vals = ""

            if not run_on_line and tx_vals != "":
                if self._trace:
                    _LOGGER.debug("Split data: [%s] [%s]", str(tx_id), str(tx_vals))
                for port in tx_vals:
                    rx_id = int(port) - (self._switch.tx_count + 1)
                    if rx_id > 0:
                        rx_list[rx_id] = int(tx_id)

            self._switch.last_response = rx_list

    def decode_vlan_luxul(self, splits):
        tx_vals = ""
        tx_id = ""
        rx_list = {}
        for line in splits:
            split_data = line.split()
            if len(split_data) >= 3 and "TX_" in split_data[1]:
                if "Gi" in split_data[2]:
                    tx_id = int(re.search('\d+', split_data[1]).group(0))
                    tx_vals = split_data[3].replace("1/", "")
                    tx_vals = self.expand_range(tx_vals)
            else:
                tx_vals = ""

            if tx_vals != "":
                if self._trace:
                    _LOGGER.debug("Split data: [%s] [%s]", str(tx_id), str(tx_vals))
                for port in tx_vals:
                    rx_id = int(port) - (self._switch.tx_count + 1)
                    if rx_id > 0:
                        rx_list[rx_id] = int(tx_id)

            self._switch.last_response = rx_list

    def get_switch_config(self):
        rx_list = {}
        if self._switch.min_refresh_interval <= (time.time() - self._switch.last_refresh):
            cmd = "show vlan\n"
            _LOGGER.debug("Rx%d: getting switch configuration", self._receiver_id)
            try:
                data = self.switch_cmd(cmd)

                if self._trace:
                    _LOGGER.debug("Rx%d: received response [%s]", self._receiver_id, data)

                if self._switch.tx_count == 0:
                    jap_config = re.search('(?<=JAP_)\d+x\d+', data[50:-1].decode()).group(0)
                    self._switch.tx_count = int(re.search('\d+', jap_config).group(0))
                    self._switch.rx_count = int(re.search('(?<=x)\d+', jap_config).group(0))
                    _LOGGER.info("Configured for Tx: %d, Rx: %d", self._switch.tx_count, self._switch.rx_count)

                splits = data[50:-1].decode().splitlines()

                if self._switch.type == 'cisco':
                    self.decode_vlan_cisco(splits)
                else:
                    self.decode_vlan_luxul(splits)

            except socket.timeout:
                _LOGGER.warning("Rx%d: switch connection timed out", self._receiver_id)
            except Exception:
                raise
        else:
            _LOGGER.debug("Rx%d: using cached switch configuration", self._receiver_id)
        
        rx_list = self._switch.last_response

        if self._trace:
            _LOGGER.debug("Rx list: [%s]", str(rx_list))

        idx = int(rx_list[self._receiver_id])

        if idx in self._transmitters:
            self._transmitter = self._transmitters[idx]
        else:
            self._transmitter = None

        self._switch.last_refresh = time.time()

    def update(self):
        """Retrieve latest state."""

        if self._rx_mac is not None:
            self.get_switch_config()

    @staticmethod
    def expand_range(s):
        r = []
        for i in s.split(','):
            if '-' not in i:
                r.append(int(i))
            else:
                l, h = map(int, i.split('-'))
                r += range(l, h + 1)
        return r

    def switch_cmd(self, cmd):
        bufsize = 1024
        timeout = 3
        data = b''

        self._switch.queue.put(threading.get_ident())
        try:
            begin = time.time()
            _LOGGER.debug("Rx%d: send switch command [%s]", self._receiver_id, cmd.encode('unicode_escape').decode())
            self.connect(self._switch.host, TELNET_PORT)
            self._switch.sock.sendall(cmd.encode())
            regexp = re.compile(r'[a-zA-z0-9]#')
            while ((time.time() - begin) < timeout) and (not regexp.search(data.decode())):
                data += self._switch.sock.recv(bufsize)
                time.sleep(0.1)
            if self._trace:
                _LOGGER.debug("Rx%d: command call took %f seconds", self._receiver_id, (time.time() - begin))
                _LOGGER.debug("Rx%d: response data is [%s]", self._receiver_id, str(data))
        finally:
            self._switch.queue.get()
        return data

    def rx_cmd(self, cmd):
        bufsize = 1024
        timeout = 3
        data = b''

        begin = time.time()
        self.connect(self._rx_ip, TELNET_PORT)
        _LOGGER.debug("Rx%d: send receiver command [%s]", self._receiver_id, cmd.encode('unicode_escape').decode())
        self._rx_sock.sendall(cmd.encode())
        regexp = re.compile(r'#')
        while ((time.time() - begin) < timeout) and (not regexp.search(data.decode())):
            data += self._rx_sock.recv(bufsize)
            time.sleep(0.1)
        if self._trace:
            _LOGGER.debug("Rx%d: command call took %f seconds", self._receiver_id, (time.time() - begin))
            _LOGGER.debug("Rx%d: response data is [%s]", self._receiver_id, str(data))

        return data

    def connect(self, host, port):
        bufsize = 1024

        if host == self._switch.host:
            try:
                data = self._switch.sock.recv(bufsize)
                if self._trace:
                    _LOGGER.debug("%s: using existing connection", host)
                    _LOGGER.debug("%s: response data [%s]", host, data)
            except socket.timeout:
                if self._trace:
                    _LOGGER.debug("%s: using existing connection", host)
                pass
            except Exception as e:
                try:
                    _LOGGER.debug("%s: connection attempt returned [%s]", host, str(e))
                    _LOGGER.info("%s: creating new connection", host)
                    self._switch.sock = socket.socket()
                    self._switch.sock.settimeout(0.2)
                    self._switch.sock.connect((host, port))
                    cmd = self._switch.user + "\r" + self._switch.password + "\r" + "terminal datadump\r"
                    self._switch.sock.sendall(cmd.encode())
                    time.sleep(1)
                    self._switch.sock.recv(bufsize)
                except Exception:
                    raise
        else:
            try:
                data = self._rx_sock.recv(bufsize)
                if self._trace:
                    _LOGGER.debug("%s: using existing connection", host)
                    _LOGGER.debug("%s: response data [%s]", host, data)
            except socket.timeout:
                if self._trace:
                    _LOGGER.debug("%s: using existing connection", host)
                pass
            except Exception as e:
                try:
                    _LOGGER.debug("%s: connection attempt returned [%s]", host, str(e))
                    _LOGGER.info("%s: creating new connection", host)
                    self._rx_sock = socket.socket()
                    self._rx_sock.settimeout(0.2)
                    self._rx_sock.connect((host, port))
                    time.sleep(0.3)
                    self._rx_sock.recv(bufsize)
                except Exception:
                    raise

    def disconnect(self):
        _LOGGER.info("Disconnecting from switch")
        self._switch.sock.close()

    def rx_disconnect(self):
        _LOGGER.info("Rx%d: disconnecting from receiver", self._receiver_id)
        self._rx_sock.close()

    @property
    def name(self):
        """Return the name of the receiver."""
        return self._receiver_name

    @property
    def state(self):
        """Return the state of the receiver."""
        return self._state

    @property
    def supported_features(self):
        """Return flag of media commands that are supported."""
        return SUPPORT_JUSTADDPOWER

    @property
    def media_image_url(self):
        """Image url of current playing media."""
        if self._transmitter[CONF_URL]:
            return self._transmitter[CONF_URL]
        elif self._image_pull:
            if self._image_pull_refresh <= (time.time() - self._image_pull_last_refresh):
                self._image_pull_last_refresh = time.time()
                _LOGGER.debug("Rx%d: updating image", self._receiver_id)
                self._image_pull_id = random.randrange(1, 100000000)
            return 'http://{0}/pull.bmp?{1}'.format(self._rx_ip, self._image_pull_id)
        else:
            return None

    @property
    def media_title(self):
        """Return the current transmitter as media title."""
        return self._transmitter[CONF_NAME]

    @property
    def source(self):
        """Return the current input transmitter of the device."""
        return self._transmitter[CONF_NAME]

    @property
    def source_list(self):
        """List of available transmitters."""
        return self._transmitter_names

    def select_source(self, transmitter):
        """Set input transmitter."""

        if transmitter not in self._transmitter_name_id:
            return
        idx = self._transmitter_name_id[transmitter]
        _LOGGER.info("Rx%d: setting source to Tx%d", self._receiver_id, idx)

        try:
            rx_port = self._receiver_id + self._switch.tx_count + 1
            tx_port = idx + 10
            if self._switch.type.lower() == 'cisco':
                cmd = "conf\r int ge{0}\r sw g al v r 11-399\r sw g al v a {1} u\r end\r".format(str(rx_port), str(tx_port))
            else:
                cmd = "conf t\r int ge{0}\r sw hy al vl rem 11-399\r sw hy al vl ad {1}\r end\r".format(str(rx_port), str(tx_port))

            try:
                data = self.switch_cmd(cmd)
                if self._trace:
                    _LOGGER.debug("Rx%d: received response [%s]", self._receiver_id, data)
            except socket.timeout:
                _LOGGER.warning("Rx%d: connection timed out", self._receiver_id)

            if self._transmitters[idx][CONF_USB]:
                _LOGGER.debug("Rx%d: setting USB connection", self._receiver_id)

                cmd = "e e_reconnect\r"
                try:
                    self.connect(self._rx_ip, TELNET_PORT)
                    time.sleep(1)
                    self.rx_cmd(cmd)
                except socket.timeout:
                    _LOGGER.warning("Rx%d: connection timed out", self._receiver_id)

            self._switch.last_response[self._receiver_id] = idx
            self._image_pull_last_refresh = 0

            return
        except Exception:
            raise
