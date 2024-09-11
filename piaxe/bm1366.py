# translated from: https://github.com/skot/ESP-Miner
import struct
import serial

import time
import math
import logging
import json
from .crc_functions import crc5, crc16_false
from . import utils
import binascii

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

class ClockManager:
    def __init__(self, bm1366, clocks, num_asics=1):
        self.bm1366 = bm1366
        self.num_asics = num_asics
        if isinstance(clocks, list):
            self.clocks = clocks
        else:
            self.clocks = [clocks for i in range(0, self.num_asics)]

    def set_clock(self, id, clock):
        logging.info(f"setting clock of {id} to {clock}")
        try:
            self.bm1366.send_hash_frequency2(id, clock)
            # all
            if id == -1:
                self.clocks = [clock for i in range(0, self.num_asics)]
            else:
                self.clocks[id] = clock
        except Exception as e:
            raise(e)

    def get_clock(self, id):
        if id == -1:
            return self.clocks
        else:
            return self.clocks[id]

    def do_frequency_ramp_up(self, frequency):
        start = current = 56.25
        step = 6.25
        target= frequency

        self.set_clock(-1, start)
        while current < target:
            next_step = min(step, target-current)
            current += next_step
            self.set_clock(-1, current)
            time.sleep(0.100)

class BM1366:
    def __init__(self):
        self.chip_id_response="aa5513660000"

    def ll_init(self, _serial_tx_func, _serial_rx_func, _reset_func):
        self.serial_tx_func = _serial_tx_func
        self.serial_rx_func = _serial_rx_func
        self.reset_func = _reset_func


    def send(self, header, data):
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

        self.serial_tx_func(buf)

    def send_simple(self, data):
        self.serial_tx_func(data)

    def send_chain_inactive(self):
        self.send(TYPE_CMD | GROUP_ALL | CMD_INACTIVE, [0x00, 0x00])

    def set_chip_address(self, chipAddr):
        self.send(TYPE_CMD | GROUP_SINGLE | CMD_SETADDRESS, [chipAddr, 0x00])

    def send_hash_frequency2(self, id, target_freq, max_diff = 0.001):
        freqbuf = bytearray([0x00, 0x08, 0x40, 0xA0, 0x02, 0x41])  # freqbuf - pll0_parameter
        postdiv_min = 255
        postdiv2_min = 255
        best = None

        for refdiv in range(2, 0, -1):
            for postdiv1 in range(7, 0, -1):
                for postdiv2 in range(7, 0, -1):
                    fb_divider = round(target_freq / 25.0 * (refdiv * postdiv2 * postdiv1))
                    newf = 25.0 * fb_divider / (refdiv * postdiv2 * postdiv1)
                    if \
                        0xa0 <= fb_divider <= 0xef and \
                        abs(target_freq - newf) < max_diff and \
                        postdiv1 >= postdiv2 and \
                        postdiv1 * postdiv2 < postdiv_min and \
                        postdiv2 <= postdiv2_min:

                            postdiv2_min = postdiv2
                            postdiv_min = postdiv1 * postdiv2
                            best = (refdiv, fb_divider, postdiv1, postdiv2, newf)

        if not best:
            raise Exception(f"didn't find PLL settings for target frequency {target_freq:.2f}")

        freqbuf[2] = 0x50 if best[1] * 25 / best[0] >= 2400 else 0x40
        freqbuf[3] = best[1]
        freqbuf[4] = best[0]
        freqbuf[5] = ((best[2] - 1) & 0xf) << 4 | (best[3] - 1) & 0xf

        if id != -1:
            freqbuf[0] = id*2
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, freqbuf)
        else:
            self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, freqbuf)

        logging.info(f"Setting Frequency to {target_freq:.2f}MHz ({best[4]:.2f})")

        return freqbuf



    def count_asic_chips(self):
        self.send(TYPE_CMD | GROUP_ALL | CMD_READ, [0x00, 0x00])

        chip_counter = 0
        while True:
            data = self.serial_rx_func(11, 5000)

            if data is None:
                break

            # only count chip id responses
            if self.chip_id_response not in binascii.hexlify(data).decode('utf8'):
                continue

            chip_counter += 1

        self.send(TYPE_CMD | GROUP_ALL | CMD_INACTIVE, [0x00, 0x00])

        return chip_counter


    def send_init(self, frequency, expected, chips_enabled = None):
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])

        chip_counter = self.count_asic_chips()

        if chip_counter != expected:
            raise Exception(f"chips mismatch. expected: {expected}, actual: {chip_counter}")

        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xa8, 0x00, 0x07, 0x00, 0x00])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x18, 0xff, 0x0f, 0xc1, 0x00])

        for id in range(0, chip_counter):
            self.set_chip_address(id * 2)

        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x3C, 0x80, 0x00, 0x85, 0x40])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x3C, 0x80, 0x00, 0x80, 0x20])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x14, 0x00, 0x00, 0x00, 0xFF])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x54, 0x00, 0x00, 0x00, 0x03])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x58, 0x02, 0x11, 0x11, 0x11])

        self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [0x00, 0x2c, 0x00, 0x7c, 0x00, 0x03])

        # change baud
        #self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x28, 0x11, 0x30, 0x02, 0x00])

        for id in range(0, chip_counter):
            if chips_enabled is not None and id not in chips_enabled:
                continue

            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0xA8, 0x00, 0x07, 0x01, 0xF0])
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x18, 0xF0, 0x00, 0xC1, 0x00])
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x3C, 0x80, 0x00, 0x85, 0x40])
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x3C, 0x80, 0x00, 0x80, 0x20])
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x3C, 0x80, 0x00, 0x82, 0xAA])
            time.sleep(0.500)

        self.clock_manager = ClockManager(self, frequency, chip_counter)
        self.clock_manager.do_frequency_ramp_up(frequency)

        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x10, 0x00, 0x00, 0x15, 0x1c])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])

        return chip_counter


    def request_chip_id(self):
        self.send_simple([0x55, 0xAA, 0x52, 0x05, 0x00, 0x00, 0x0A]) # chipid


    def send_read_address(self):
        self.send(TYPE_CMD | GROUP_ALL | CMD_READ, [0x00, 0x00])

    def reset(self):
        self.reset_func()

    def init(self, frequency, expected, chips_enabled = None):
        logging.info("Initializing BM1366")

        self.reset()

        return self.send_init(frequency, expected, chips_enabled)

    # Baud formula = 25M/((denominator+1)*8)
    # The denominator is 5 bits found in the misc_control (bits 9-13)
    def set_default_baud(self):
        # default divider of 26 (11010) for 115,749
        baudrate = [0x00, MISC_CONTROL, 0x00, 0x00, 0b01111010, 0b00110001]
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, baudrate, 6)
        return 115749

    def set_max_baud(self):
        # Log the setting of max baud (you would need to have a logging mechanism in place)
        logging.info("Setting max baud of 1000000")

        # divider of 0 for 3,125,000
        init8 = [0x55, 0xAA, 0x51, 0x09, 0x00, 0x28, 0x11, 0x30, 0x02, 0x00, 0x03]
        self.send_simple(init8, 11)
        return 1000000

    def _largest_power_of_two(self, n):
        # Finds the largest power of 2 less than or equal to n
        p = 1
        while p * 2 <= n:
            p *= 2
        return p

    def _reverse_bits(self, byte):
        # Reverses the bits in a byte
        return int('{:08b}'.format(byte)[::-1], 2)

    def set_job_difficulty_mask(self, difficulty):
        # Default mask of 256 diff
        job_difficulty_mask = [0x00, TICKET_MASK, 0b00000000, 0b00000000, 0b00000000, 0b11111111]

        # The mask must be a power of 2 so there are no holes
        # Correct:  {0b00000000, 0b00000000, 0b11111111, 0b11111111}
        # Incorrect: {0b00000000, 0b00000000, 0b11100111, 0b11111111}
        # (difficulty - 1) if it is a pow 2 then step down to second largest for more hashrate sampling
        difficulty = self._largest_power_of_two(difficulty) - 1

        # convert difficulty into char array
        # Ex: 256 = {0b00000000, 0b00000000, 0b00000000, 0b11111111}, {0x00, 0x00, 0x00, 0xff}
        # Ex: 512 = {0b00000000, 0b00000000, 0b00000001, 0b11111111}, {0x00, 0x00, 0x01, 0xff}
        for i in range(4):
            value = (difficulty >> (8 * i)) & 0xFF
            # The char is read in backwards to the register so we need to reverse them
            # So a mask of 512 looks like 0b00000000 00000000 00000001 1111111
            # and not 0b00000000 00000000 10000000 1111111
            job_difficulty_mask[5 - i] = self._reverse_bits(value)

        # Log the setting of job ASIC mask (replace with your logging method)
        logging.info("Setting job ASIC mask to %d", difficulty)

        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, job_difficulty_mask)

    def send_work(self, t: WorkRequest):
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
        #logging.debug("%s", bytearray(job_packet_data).hex())

        self.send((TYPE_JOB | GROUP_SINGLE | CMD_WRITE), job_packet_data)

    def get_job_id_from_result(self, job_id):
        return job_id & 0xf8

    def get_job_id(self, job_id):
        return ((job_id << 3) & 0x7f) + 0x08

    def receive_work(self, timeout=100):
        # Read 11 bytes from serial port
        asic_response_buffer = self.serial_rx_func(11, timeout)

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

    def try_get_temp_from_response(self, response : AsicResult):
        return (None, None)


class BM1368(BM1366):
    def __init__(self):
        self.chip_id_response="aa5513680000"

    def get_job_id_from_result(self, job_id):
        return (job_id & 0xf0) >> 1

    def get_job_id(self, job_id):
        # job-IDs: 00, 18, 30, 48, 60, 78, 10, 28, 40, 58, 70, 08, 20, 38, 50, 68
        return (job_id * 24) & 0x7f

    def clear_serial_buffer(self):
        while True:
            data = self.serial_rx_func(11, 5000)
            if data is None:
                return

    def send_init(self, frequency, expected, chips_enabled = None):
        self.clear_serial_buffer()

        # enable and set version rolling mask to 0xFFFF
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])
        # enable and set version rolling mask to 0xFFFF again
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])
        # enable and set version rolling mask to 0xFFFF again
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])
        # enable and set version rolling mask to 0xFFFF again
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])

        chip_counter = self.count_asic_chips()

        if chip_counter != expected:
            raise Exception(f"chips mismatch. expected: {expected}, actual: {chip_counter}")

        # enable and set version rolling mask to 0xFFFF again
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])
        # Reg_A8
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xa8, 0x00, 0x07, 0x00, 0x00])
        # Misc Control
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x18, 0xff, 0x0f, 0xc1, 0x00])

        for id in range(0, chip_counter):
            self.set_chip_address(id * 2)

        # Core Register Control
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x3C, 0x80, 0x00, 0x8b, 0x00])
        # Core Register Control
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x3C, 0x80, 0x00, 0x80, 0x18])
        # set ticket mask
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x14, 0x00, 0x00, 0x00, 0xFF])
        # Analog Mux Control
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x54, 0x00, 0x00, 0x00, 0x03])
        # Set the IO Driver Strength
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x58, 0x02, 0x11, 0x11, 0x11])

        for id in range(0, chip_counter):
            if chips_enabled is not None and id not in chips_enabled:
                continue

            # Reg_A8
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0xA8, 0x00, 0x07, 0x01, 0xF0])
            # Misc Control
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x18, 0xF0, 0x00, 0xC1, 0x00])
            # Core Register Control
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x3C, 0x80, 0x00, 0x8b, 0x00])
            # Core Register Control
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x3C, 0x80, 0x00, 0x80, 0x18])
            # Core Register Control
            self.send(TYPE_CMD | GROUP_SINGLE | CMD_WRITE, [id*2, 0x3C, 0x80, 0x00, 0x82, 0xAA])
            time.sleep(0.500)

        self.clock_manager = ClockManager(self, frequency, chip_counter)
        self.clock_manager.do_frequency_ramp_up(frequency)

        # change baud
        #self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x28, 0x11, 0x30, 0x02, 0x00])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0x10, 0x00, 0x00, 0x15, 0xa4])
        self.send(TYPE_CMD | GROUP_ALL | CMD_WRITE, [0x00, 0xA4, 0x90, 0x00, 0xFF, 0xFF])

        return chip_counter


    def request_temps(self):
        self.send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xB0, 0x80, 0x00, 0x00, 0x00, 0x0F])
        self.send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xB0, 0x00, 0x02, 0x00, 0x00, 0x1F])
        self.send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xB0, 0x01, 0x02, 0x00, 0x00, 0x16])
        self.send_simple([0x55, 0xAA, 0x51, 0x09, 0x00, 0xB0, 0x10, 0x02, 0x00, 0x00, 0x1B])
        self.send_simple([0x55, 0xAA, 0x52, 0x05, 0x00, 0xB4, 0x1B])

    def try_get_temp_from_response(self, response : AsicResult):
        # temp response has this pattern
        # aa55 8000080c 00 b4 0000 1a
        if response.nonce & 0x0000ffff == 0x00000080 and response.job_id == 0xb4:
            value = (response.nonce & 0xff000000) >> 24 | (response.nonce & 0x00ff0000) >> 8
            id = response.midstate_num // 2

            return (value, id)

        return (None, None)
