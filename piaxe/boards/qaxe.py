import logging
import threading
import serial
import time
import binascii
import os

try:
    from . import coms_pb2
except:
    pass

from . import board

class QaxeHardware(board.Board):
    def __init__(self, config):
        # Load settings from config
        self.config = config

        self.state_power = 0;
        self.pwm1 = self.config.get('fan_speed_1', 100)
        self.pwm2 = self.config.get('fan_speed_2', 0)

        self.reqid = 0
        self.serial_port_ctrl_lock = threading.Lock()

        serial = config.get("serial_number", None)
        serial_port_asic = config.get("serial_port_asic", None)
        serial_port_ctrl = config.get("serial_port_ctrl", None)

        if serial:
            path = f"/dev/serial/by-id/usb-Microengineer_Qaxe_{serial}-if"
            try:
                serial_port_asic = os.path.realpath(f"{path}00")
                serial_port_ctrl = os.path.realpath(f"{path}02")
            except Exception as e:
                raise Exception(f"couldn't resolve path: %s", e)

        if not serial_port_asic or not serial_port_ctrl:
            raise Exception(f"serial ports not specified")

        logging.info(f"serial_port_asic: {serial_port_asic}")
        logging.info(f"serial_port_ctrl: {serial_port_ctrl}")

        # Initialize serial communication
        self._serial_port_asic = serial.Serial(
            port=serial_port_asic,  # For GPIO serial communication use /dev/ttyS0
            baudrate=115200,    # Set baud rate to 115200
            bytesize=serial.EIGHTBITS,     # Number of data bits
            parity=serial.PARITY_NONE,     # No parity
            stopbits=serial.STOPBITS_ONE,  # Number of stop bits
            timeout=1                      # Set a read timeout
        )

        # Initialize serial communication
        self._serial_port_ctrl = serial.Serial(
            port=serial_port_ctrl,  # For GPIO serial communication use /dev/ttyS0
            baudrate=115200,    # Set baud rate to 115200
            bytesize=serial.EIGHTBITS,     # Number of data bits
            parity=serial.PARITY_NONE,     # No parity
            stopbits=serial.STOPBITS_ONE,  # Number of stop bits
            timeout=1                      # Set a read timeout
        )

        self._switch_power(True)

    def _is_power_good(self):
        return True

    def set_fan_speed(self, channel, speed):
        if channel == 0:
            self.pwm1 = speed
        elif channel == 1:
            self.pwm2 = speed
        self._set_state()

    def set_led(self, state):
        pass

    def _request(self, op, params):
        request = coms_pb2.QRequest()
        request.id = self.reqid  # Set a unique ID for the request
        request.op = op

        if params is not None:
            request.data = params.SerializeToString()
        else:
            request.data = b'0x00'
        request.data = bytes([len(request.data)]) + request.data

        serialized_request = request.SerializeToString()
        serialized_request = bytes([len(serialized_request)]) + serialized_request

        logging.debug("-> %s", binascii.hexlify(serialized_request).decode('utf8'))

        self._serial_port_ctrl.write(serialized_request)

        response_len = self._serial_port_ctrl.read()
        logging.debug(f"rx len: {response_len}")
        if len(response_len) == 1 and response_len[0] == 0:
            self.reqid += 1
            return coms_pb2.QResponse()

        response_data = self._serial_port_ctrl.read(response_len[0])

        logging.debug("<- %s", binascii.hexlify(response_data).decode('utf8'))

        response = coms_pb2.QResponse()
        response.ParseFromString(response_data)

        if response.id != self.reqid:
            logging.error(f"request and response IDs mismatch! {response.id} vs {self.reqid}")

        self.reqid += 1
        return response

    def read_temperature_and_voltage(self):
        with self.serial_port_ctrl_lock:
            resp = self._request(2, None)
            if resp is None or resp.error != 0:
                raise Exception("failed reading status!")

            status = coms_pb2.QState()
            status.ParseFromString(resp.data[1:])

            return {
                "temp": [status.temp1 * 0.0625, status.temp2 * 0.0625, None, None],
                "voltage": [None, None, None, None],
            }

    def _set_state(self):
        with self.serial_port_ctrl_lock:
            qcontrol = coms_pb2.QControl()
            qcontrol.state_1v2 = self.state
            qcontrol.pwm1 = int(min(100, self.pwm1 * 100.0))
            qcontrol.pwm2 = int(min(100, self.pwm2 * 100.0))
            if self._request(1, qcontrol).error != 0:
                raise Exception("couldn't switch power!")

    def _switch_power(self, state):
        self.state = 0 if not state else 1
        self._set_state()

        time.sleep(5)

    def reset_func(self):
        with self.serial_port_ctrl_lock:
            # cmd reset
            if self._request(3, None).error != 0:
                raise Exception("error resetting qaxe!")
            time.sleep(5)

    def shutdown(self):
        # disable buck converter
        logging.info("shutdown miner ...")
        with self.serial_port_ctrl_lock:
            # cmd shutdown
            if self._request(4, None).error != 0:
                raise Exception("error resetting qaxe!")

    def serial_port(self):
        return self._serial_port_asic
