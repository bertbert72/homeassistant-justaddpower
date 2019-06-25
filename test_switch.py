import socket
import time
import re
import types
import queue
import threading

CONF_NAME = 'name'
TELNET_PORT = 23

switch = types.SimpleNamespace()
switch.host = 'japluxul01'
switch.user = 'cisco'
switch.password = 'cisco'
switch.min_refresh_interval = 0
switch.sock = None
switch.queue = queue.Queue(1)
switch.last_refresh = 0
switch.last_response = ""
switch.tx_count = 0
switch.rx_count = 0
switch.type = 'luxul'


class JustaddpowerReceiver(object):
    """Representation of a Just Add Power receiver."""

    def __init__(self, switch, transmitters, receiver_id, receiver_name):
        """Initialize new receiver."""

        self._transmitter = None
        self._transmitters = transmitters
        self._transmitter_name_id = {v[CONF_NAME]: k for k, v in transmitters.items()}
        self._transmitter_names = sorted(self._transmitter_name_id.keys(), key=lambda v: self._transmitter_name_id[v])
        self._receiver_id = receiver_id
        self._receiver_name = receiver_name
        self._trace = True
        self._switch = switch

        self.get_switch_config()

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
                    print(f"Split data: [{tx_id}] {tx_vals}")
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
                    print(f"Split data: [{tx_id}] {tx_vals}")
                for port in tx_vals:
                    rx_id = int(port) - (self._switch.tx_count + 1)
                    if rx_id > 0:
                        rx_list[rx_id] = int(tx_id)

            self._switch.last_response = rx_list

    def get_switch_config(self):
        if self._switch.min_refresh_interval <= (time.time() - self._switch.last_refresh):
            cmd = "show vlan\n"
            print(f"Rx{self._receiver_id}: getting switch configuration")
            try:
                data = self.switch_cmd(cmd)

                if self._trace:
                    print(f"Rx{self._receiver_id}: received response [{data}]")

                if self._switch.tx_count == 0:
                    jap_config = re.search('(?<=JAP_)\d+x\d+', data[50:-1].decode()).group(0)
                    self._switch.tx_count = int(re.search('\d+', jap_config).group(0))
                    self._switch.rx_count = int(re.search('(?<=x)\d+', jap_config).group(0))
                    print(f"Configured for Tx: {self._switch.tx_count}, Rx: {self._switch.rx_count}")

                splits = data[50:-1].decode().splitlines()

                if self._switch.type == 'cisco':
                    self.decode_vlan_cisco(splits)
                else:
                    self.decode_vlan_luxul(splits)

            except socket.timeout:
                print(f"Rx{self._receiver_id}: switch connection timed out")
            except Exception:
                raise
        else:
            print(f"Rx{self._receiver_id}: using cached switch configuration")

        rx_list = self._switch.last_response

        if self._trace:
            print(f"Rx list: [{rx_list}]")

        idx = int(rx_list[self._receiver_id])

        if idx in self._transmitters:
            self._transmitter = self._transmitters[idx]
        else:
            self._transmitter = None

        self._switch.last_refresh = time.time()

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
            print(f"Rx{self._receiver_id}: send switch command [{cmd.encode('unicode_escape').decode()}]")
            self.connect(self._switch.host, TELNET_PORT)
            self._switch.sock.sendall(cmd.encode())
            regexp = re.compile(r'[a-zA-z0-9]#')
            while ((time.time() - begin) < timeout) and (not regexp.search(data.decode())):
                data += self._switch.sock.recv(bufsize)
                time.sleep(0.1)
            if self._trace:
                print(f"Rx{self._receiver_id}: command call took {(time.time() - begin)} seconds")
                print(f"Rx{self._receiver_id}: response data is [{data}]")
        finally:
            self._switch.queue.get()
        return data

    def connect(self, host, port):
        bufsize = 1024

        try:
            data = self._switch.sock.recv(bufsize)
            if self._trace:
                print(f"{host}: using existing connection")
                print(f"{host}: response data [{data}]")
        except socket.timeout:
            if self._trace:
                print(f"{host}: using existing connection")
            pass
        except Exception as e:
            try:
                print(f"{host}: connection attempt returned [{e}]")
                print(f"{host}: creating new connection", )
                self._switch.sock = socket.socket()
                self._switch.sock.settimeout(0.2)
                self._switch.sock.connect((host, port))
                cmd = self._switch.user + "\r" + self._switch.password + "\r" + "terminal datadump\r"
                self._switch.sock.sendall(cmd.encode())
                time.sleep(1)
                self._switch.sock.recv(bufsize)
            except Exception:
                raise

    def disconnect(self):
        print(f"Disconnecting from switch")
        self._switch.sock.close()


config = JustaddpowerReceiver(switch, {1: {'name': 'Cable1'}, 2: {'name': 'Cable2'}}, 1, 'Reception')
