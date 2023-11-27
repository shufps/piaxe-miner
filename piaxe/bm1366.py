# translated from: https://github.com/skot/ESP-Miner
import struct
import serial

import time
import math
import logging
import json
from .crc_functions import crc5, crc16_false
from . import utils

TYPE_JOB = 0x20
TYPE_CMD = 0x40

JOB_PACKET = 0
CMD_PACKET = 1

GROUP_SINGLE = 0x00
GROUP_ALL = 0x10

CMD_JOB = 0x01

CMD_SETADDRESS = 0x00
CMD_WRITE = 0x01
CMD_READ = 0x02
CMD_INACTIVE = 0x03

RESPONSE_CMD = 0x00
RESPONSE_JOB = 0x80

SLEEP_TIME = 20
FREQ_MULT = 25.0

CLOCK_ORDER_CONTROL_0 = 0x80
CLOCK_ORDER_CONTROL_1 = 0x84
ORDERED_CLOCK_ENABLE = 0x20
CORE_REGISTER_CONTROL = 0x3C
PLL3_PARAMETER = 0x68
FAST_UART_CONFIGURATION = 0x28
TICKET_MASK = 0x14
MISC_CONTROL = 0x18

serial_tx_func = None
serial_rx_func = None
reset_func = None

class AsicResult:
    # Define the struct format corresponding to the C structure.
    # < for little-endian, B for uint8_t, I for uint32_t, H for uint16_t
    _struct_format = '<2BIBBHB'

    def __init__(self):
        self.preamble = [0x00, 0x00]
        self.nonce = 0
        self.midstate_num = 0
        self.job_id = 0
        self.version = 0
        self.crc = 0

    @classmethod
    def from_bytes(cls, data):
        # Unpack the data using the struct format.
        unpacked_data = struct.unpack(cls._struct_format, data)

        # Create an instance of the AsicResult class.
        result = cls()

        # Assign the unpacked data to the class fields.
        result.preamble = list(unpacked_data[0:2])
        result.nonce = unpacked_data[2]
        result.midstate_num = unpacked_data[3]
        result.job_id = unpacked_data[4]
        result.version = unpacked_data[5]
        result.crc = unpacked_data[6]

        return result

    def print(self):
        print("AsicResult:")
        print(f"  preamble:        {self.preamble}")
        print(f"  nonce:           {self.nonce:08x}")
        print(f"  midstate_num:    {self.midstate_num}")
        print(f"  job_id:          {self.job_id:02x}")
        print(f"  version:         {self.version:04x}")
        print(f"  crc:             {self.crc:02x}")

class WorkRequest:
    def __init__(self):
        self.time = None
        self.id  = int(0)
        self.starting_nonce = int(0)
        self.nbits = int(0)
        self.ntime = int(0)
        self.merkle_root = bytearray([])
        self.prev_block_hash = bytearray([])
        self.version = int(0)

    def create_work(self, id, starting_nonce, nbits, ntime, merkle_root, prev_block_hash, version):
        self.time = time.time()
        self.id = id
        self.starting_nonce = starting_nonce
        self.nbits = nbits
        self.ntime = ntime
        self.merkle_root = merkle_root
        self.prev_block_hash = prev_block_hash
        self.version = version

    def print(self):
        print("WorkRequest:")
        print(f"  id:              {self.id:02x}")
        print(f"  starting_nonce:  {self.starting_nonce:08x}")
        print(f"  nbits:           {self.nbits:08x}")
        print(f"  ntime:           {self.ntime:08x}")
        print(f"  merkle_root:     {self.merkle_root.hex()}")
        print(f"  prev_block_hash: {self.prev_block_hash.hex()}")
        print(f"  version:         {self.version:08x}")



class TaskResult:
    def __init__(self, job_id, nonce, rolled_version):
        self.job_id = job_id
        self.nonce = nonce
        self.rolled_version = rolled_version


def ll_init(_serial_tx_func, _serial_rx_func, _reset_func):
    global serial_tx_func, serial_rx_func, reset_func
    serial_tx_func = _serial_tx_func
    serial_rx_func = _serial_rx_func
    reset_func = _reset_func


def send_BM1366(header, data, debug=False):
    packet_type = JOB_PACKET if header & TYPE_JOB else CMD_PACKET
    data_len = len(data)
    total_length = data_len + 6 if packet_type == JOB_PACKET else data_len + 5

    # Create a buffer
    buf = bytearray(total_length)

    # Add the preamble
    buf[0] = 0x55
    buf[1] = 0xAA

    # Add the header field
    buf[2] = header

    # Add the length field
    buf[3] = data_len + 4 if packet_type == JOB_PACKET else data_len + 3

    # Add the data
    buf[4:data_len+4] = data

    # Add the correct CRC type
    if packet_type == JOB_PACKET:
        crc16_total = crc16_false(buf[2:data_len+4])
        buf[4 + data_len] = (crc16_total >> 8) & 0xFF
        buf[5 + data_len] = crc16_total & 0xFF
    else:
        buf[4 + data_len] = crc5(buf[2:data_len+4])

    serial_tx_func(buf, debug)

def send_simple(data):
    serial_tx_func(data)

def send_chain_inactive():
    send_BM1366(TYPE_CMD | GROUP_ALL | CMD_INACTIVE, [0x00, 0x00])

def set_chip_address(chipAddr):
    send_BM1366(TYPE_CMD | GROUP_SINGLE | CMD_SETADDRESS, [chipAddr, 0x00])

def send_hash_frequency(target_freq):
    # default 200Mhz if it fails
    freqbuf = bytearray([0x00, 0x08, 0x40, 0xA0, 0x02, 0x41])  # freqbuf - pll0_parameter
    newf = 200.0

    fb_divider = 0
    post_divider1 = post_divider2 = ref_divider = 0
    min_difference = 10.0

    # Calculate dividers
    for refdiv_loop in range(2, 0, -1):
        if fb_divider != 0:
            break
        for postdiv1_loop in range(7, 0, -1):
            if fb_divider != 0:
                break
            for postdiv2_loop in range(1, postdiv1_loop):
                temp_fb_divider = round(postdiv1_loop * postdiv2_loop * target_freq * refdiv_loop / 25.0)

                if 144 <= temp_fb_divider <= 235:
                    temp_freq = 25.0 * temp_fb_divider / (refdiv_loop * postdiv2_loop * postdiv1_loop)
                    freq_diff = abs(target_freq - temp_freq)

                    if freq_diff < min_difference:
                        fb_divider = temp_fb_divider
                        post_divider1 = postdiv1_loop
                        post_divider2 = postdiv2_loop
                        ref_divider = refdiv_loop
                        min_difference = freq_diff
                        break

    if fb_divider == 0:
        logging.info("Finding dividers failed, using default value (200Mhz)")
    else:
        newf = 25.0 / ref_divider / fb_divider / (post_divider1 * post_divider2)
        logging.info(f"final refdiv: {ref_divider}, fbdiv: {fb_divider}, postdiv1: {post_divider1}, postdiv2: {post_divider2}, min diff value: {min_difference}")

        freqbuf[3] = fb_divider
        freqbuf[4] = ref_divider
        freqbuf[5] = ((post_divider1 - 1) & 0xf) << 4 | (post_divider2 - 1) & 0xf

        if fb_divider * 25 / ref_divider >= 2400:
            freqbuf[2] = 0x50

    # Send the frequency buffer
    send_BM1366(TYPE_CMD | GROUP_ALL | CMD_WRITE, freqbuf, debug=True)

    # Log the result
    logging.info(f"Setting Frequency to {target_freq:.2f}MHz ({newf:.2f})")



def do_frequency_ramp_up():
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA2, 0x02, 0x55, 0x0F])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAF, 0x02, 0x64, 0x08])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA5, 0x02, 0x54, 0x08])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x63, 0x11])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB6, 0x02, 0x63, 0x0C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x53, 0x1A])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x53, 0x12])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x62, 0x14])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAA, 0x02, 0x43, 0x15])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA2, 0x02, 0x52, 0x14])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAB, 0x02, 0x52, 0x12])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x52, 0x17])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBD, 0x02, 0x52, 0x11])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA5, 0x02, 0x42, 0x0C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA1, 0x02, 0x61, 0x1D])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x61, 0x1B])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAF, 0x02, 0x61, 0x19])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB6, 0x02, 0x61, 0x06])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA2, 0x02, 0x51, 0x1B])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x51, 0x10])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAE, 0x02, 0x51, 0x0A])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x51, 0x18])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBA, 0x02, 0x51, 0x1C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA0, 0x02, 0x41, 0x14])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA5, 0x02, 0x41, 0x03])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAA, 0x02, 0x41, 0x1F])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAF, 0x02, 0x41, 0x08])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x41, 0x02])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB9, 0x02, 0x41, 0x0B])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBE, 0x02, 0x41, 0x09])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xC3, 0x02, 0x41, 0x01])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA0, 0x02, 0x31, 0x18])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA4, 0x02, 0x31, 0x17])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x31, 0x06])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAC, 0x02, 0x31, 0x09])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB0, 0x02, 0x31, 0x01])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x31, 0x0E])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA1, 0x02, 0x60, 0x18])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBC, 0x02, 0x31, 0x10])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x60, 0x1E])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xC4, 0x02, 0x31, 0x0F])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAF, 0x02, 0x60, 0x1C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xCC, 0x02, 0x31, 0x11])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB6, 0x02, 0x60, 0x03])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xD4, 0x02, 0x31, 0x16])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA2, 0x02, 0x50, 0x1E])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA5, 0x02, 0x50, 0x1C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA8, 0x02, 0x50, 0x15])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAB, 0x02, 0x50, 0x18])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAE, 0x02, 0x50, 0x0F])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB1, 0x02, 0x50, 0x0A])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x50, 0x1D])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB7, 0x02, 0x50, 0x10])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBA, 0x02, 0x50, 0x19])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBD, 0x02, 0x50, 0x1B])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA0, 0x02, 0x40, 0x11])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xC3, 0x02, 0x50, 0x1E])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xA5, 0x02, 0x40, 0x06])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xC9, 0x02, 0x50, 0x15])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAA, 0x02, 0x40, 0x1A])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xCF, 0x02, 0x50, 0x0F])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xAF, 0x02, 0x40, 0x0D])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xD5, 0x02, 0x50, 0x1D])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB4, 0x02, 0x40, 0x07])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xDB, 0x02, 0x50, 0x19])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xB9, 0x02, 0x40, 0x0E])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xE1, 0x02, 0x50, 0x1C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x40, 0xBE, 0x02, 0x40, 0x0C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xE7, 0x02, 0x50, 0x06])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x08, 0x50, 0xC2, 0x02, 0x40, 0x1C])

def send_init(frequency):
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF, 0x1C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF, 0x1C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF, 0x1C])
    send_simple([0x55, 0xAA, 0x52, 0x05, 0x00, 0x00, 0x0A]) # chipid
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xA8, 0x00, 0x07, 0x00, 0x00, 0x03])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x18, 0xFF, 0x0F, 0xC1, 0x00, 0x00])
    send_simple([0x55, 0xAA, 0x53, 0x05, 0x00, 0x00, 0x03])
    send_simple([0x55, 0xAA, 0x40, 0x05, 0x00, 0x00, 0x1C])

    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x3C, 0x80, 0x00, 0x85, 0x40, 0x0C])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x3C, 0x80, 0x00, 0x80, 0x20, 0x19])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x14, 0x00, 0x00, 0x00, 0xFF, 0x08])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x54, 0x00, 0x00, 0x00, 0x03, 0x1D])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x58, 0x02, 0x11, 0x11, 0x11, 0x06])

    send_simple([0x55, 0xAA, 0x41, 0x09, 0x00, 0x2C, 0x00, 0x7C, 0x00, 0x03, 0x03])
#    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x28, 0x11, 0x30, 0x02, 0x00, 0x03]) change baud?
    send_simple([0x55, 0xAA, 0x41, 0x09, 0x00, 0xA8, 0x00, 0x07, 0x01, 0xF0, 0x15])
    send_simple([0x55, 0xAA, 0x41, 0x09, 0x00, 0x18, 0xF0, 0x00, 0xC1, 0x00, 0x0C])
    send_simple([0x55, 0xAA, 0x41, 0x09, 0x00, 0x3C, 0x80, 0x00, 0x85, 0x40, 0x04])
    send_simple([0x55, 0xAA, 0x41, 0x09, 0x00, 0x3C, 0x80, 0x00, 0x80, 0x20, 0x11])
    send_simple([0x55, 0xAA, 0x41, 0x09, 0x00, 0x3C, 0x80, 0x00, 0x82, 0xAA, 0x05])

    send_simple([0x55, 0xAA, 0x52, 0x05, 0x00, 0x00, 0x0A])

    do_frequency_ramp_up();

    send_hash_frequency(frequency);

    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0x10, 0x00, 0x00, 0x15, 0x1C, 0x02])
    send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF, 0x1C])

def request_chip_id():
    send_simple([0x55, 0xAA, 0x52, 0x05, 0x00, 0x00, 0x0A]) # chipid


def send_read_address():
    send_BM1366(TYPE_CMD | GROUP_ALL | CMD_READ, [0x00, 0x00])

def reset():
    reset_func()

def init(frequency):
    logging.info("Initializing BM1366")

    reset();

    # send the init command
    #_send_read_address();

    send_init(frequency);

# Baud formula = 25M/((denominator+1)*8)
# The denominator is 5 bits found in the misc_control (bits 9-13)
def set_default_baud():
    # default divider of 26 (11010) for 115,749
    baudrate = [0x00, MISC_CONTROL, 0x00, 0x00, 0b01111010, 0b00110001]
    send_BM1366(TYPE_CMD | GROUP_ALL | CMD_WRITE, baudrate, 6, False)
    return 115749

def set_max_baud():
    # Log the setting of max baud (you would need to have a logging mechanism in place)
    logging.info("Setting max baud of 1000000")

    # divider of 0 for 3,125,000
    init8 = [0x55, 0xAA, 0x51, 0x09, 0x00, 0x28, 0x11, 0x30, 0x02, 0x00, 0x03]
    send_simple(init8, 11)
    return 1000000

def largest_power_of_two(n):
    # Finds the largest power of 2 less than or equal to n
    p = 1
    while p * 2 <= n:
        p *= 2
    return p

def reverse_bits(byte):
    # Reverses the bits in a byte
    return int('{:08b}'.format(byte)[::-1], 2)

def set_job_difficulty_mask(difficulty):
    # Default mask of 256 diff
    job_difficulty_mask = [0x00, TICKET_MASK, 0b00000000, 0b00000000, 0b00000000, 0b11111111]

    # The mask must be a power of 2 so there are no holes
    # Correct:  {0b00000000, 0b00000000, 0b11111111, 0b11111111}
    # Incorrect: {0b00000000, 0b00000000, 0b11100111, 0b11111111}
    # (difficulty - 1) if it is a pow 2 then step down to second largest for more hashrate sampling
    difficulty = largest_power_of_two(difficulty) - 1

    # convert difficulty into char array
    # Ex: 256 = {0b00000000, 0b00000000, 0b00000000, 0b11111111}, {0x00, 0x00, 0x00, 0xff}
    # Ex: 512 = {0b00000000, 0b00000000, 0b00000001, 0b11111111}, {0x00, 0x00, 0x01, 0xff}
    for i in range(4):
        value = (difficulty >> (8 * i)) & 0xFF
        # The char is read in backwards to the register so we need to reverse them
        # So a mask of 512 looks like 0b00000000 00000000 00000001 1111111
        # and not 0b00000000 00000000 10000000 1111111
        job_difficulty_mask[5 - i] = reverse_bits(value)

    # Log the setting of job ASIC mask (replace with your logging method)
    logging.info("Setting job ASIC mask to %d", difficulty)

    send_BM1366(TYPE_CMD | GROUP_ALL | CMD_WRITE, job_difficulty_mask, False)

def send_work(t: WorkRequest):
    job_packet_format = '<B B I I I 32s 32s I'
    job_packet_data = struct.pack(
        job_packet_format,
        t.id,
        0x01,  # num_midstates
        t.starting_nonce,
        t.nbits,
        t.ntime,
        t.merkle_root,
        t.prev_block_hash,
        t.version
    )
    logging.debug("%s", bytearray(job_packet_data).hex())

    send_BM1366((TYPE_JOB | GROUP_SINGLE | CMD_WRITE), job_packet_data)

def receive_work(timeout=100):
    # Read 11 bytes from serial port
    asic_response_buffer = serial_rx_func(11, timeout)

    # Check for valid response
    if not asic_response_buffer:
        # Didn't find a solution, restart and try again
        return None

    if len(asic_response_buffer) != 11 or asic_response_buffer[0:2] != b'\xAA\x55':
        logging.info(f"Serial RX invalid {len(asic_response_buffer)}")
        logging.info(f"{asic_response_buffer.hex()}")
        return None

    # Unpack the buffer into an AsicResult object
    asic_result = AsicResult().from_bytes(asic_response_buffer)
    return asic_result


# Function to reverse the bytes in a 16-bit number
def reverse_uint16(num):
    return ((num >> 8) | (num << 8)) & 0xFFFF

