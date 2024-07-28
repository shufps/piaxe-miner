

import serial
import time
import logging
import random
import copy
import os
import math
import yaml
import json

import threading
from shared import shared

from . import ssd1306
from . import bm1366
from . import influx
from . import discord
from . import rest
from . import smartplug

from .boards import piaxe
from .boards import qaxe
from .boards import bitcrane
from .boards import flex4axe
from .boards import zeroxaxe

try:
    from .ssd1306 import SSD1306
except:
    pass

class Job(shared.Job):
    def __init__(
        self,
        job_id,
        prevhash,
        coinb1,
        coinb2,
        merkle_branches,
        version,
        nbits,
        ntime,
        extranonce1,
        extranonce2_size,
        max_nonce=0x7fffffff,
    ):
        super().__init__(job_id, prevhash, coinb1, coinb2, merkle_branches, version, nbits, ntime, extranonce1, extranonce2_size, max_nonce)


class BM1366Miner:
    def __init__(self, config, address, network):
        self.config = config

        self.current_job = None
        self.current_work = None
        self.serial_port = None

        self._read_index = 0
        self._write_index = 0
        self._buffer = bytearray([0] * 64)

        self._internal_id = 0
        self._latest_work_id = 0
        self._jobs = dict()
        self.last_response = time.time()

        self.tracker_send = list()
        self.tracker_received = list()

        self.job_thread = None
        self.receive_thread = None
        self.temp_thread = None
        self.display_thread = None
        self.job_lock = threading.Lock()
        self.serial_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.new_job_event = threading.Event()
        self.led_thread = None
        self.led_event = threading.Event()
        self.network = network
        self.address = address

        self.last_job_time = time.time()
        self.last_response = time.time()

        self.found_hashes = dict()
        self.found_timestamps = list()

        self.shares = list()
        self.stats = influx.Stats()

        self.display = SSD1306(self.stats)

        self.miner = self.config['miner']
        self.verify_solo = self.config.get('verify_solo', False)
        self.debug_bm1366 = self.config.get("debug_bm1366", False)

    def shutdown(self):
        # signal the threads to end
        self.stop_event.set()

        # stop influx
        if self.influx:
            self.influx.shutdown()

        # stop smartplug
        if self.smartplug:
            self.smartplug.shutdown()

        # join all threads
        for t in [self.job_thread, self.receive_thread, self.temp_thread, self.display_thread, self.led_thread, self.uptime_counter_thread, self.alerter_thread]:
            if t is not None:
                t.join(5)

        self.hardware.shutdown()

    def get_name(self):
        return self.hardware.get_name()

    def get_user_agent(self):
        return f"{self.get_name()}/0.1"

    def init(self):
        if self.miner == 'bitcrane':
            self.hardware = bitcrane.BitcraneHardware(self.config[self.miner])
            self.asics = bm1366.BM1366()
        if self.miner == 'piaxe':
            self.hardware = piaxe.RPiHardware(self.config[self.miner])
            self.asics = bm1366.BM1366()
        elif self.miner == "qaxe":
            self.hardware = qaxe.QaxeHardware(self.config[self.miner])
            self.asics = bm1366.BM1366()
        elif self.miner == "qaxe+":
            self.hardware = qaxe.QaxeHardware(self.config[self.miner])
            self.asics = bm1366.BM1368()
        elif self.miner == "flex4axe":
            self.hardware = flex4axe.Flex4AxeHardware(self.config[self.miner])
            self.asics = bm1366.BM1366()
        elif self.miner == "0xaxe":
            self.hardware = zeroxaxe.ZeroxAxe(self.config[self.miner])
            self.asics = bm1366.BM1366()
        else:
            raise Exception('unknown miner: %s', self.miner)

        self.serial_port = self.hardware.serial_port()

        # set the hardware dependent functions for serial and reset
        self.asics.ll_init(self._serial_tx_func, self._serial_rx_func,
                       self.hardware.reset_func)


        # default is: enable all chips
        chips_enabled = self.config[self.miner].get('chips_enabled', None)


        max_retries = 5  # Maximum number of attempts

        # currently the qaxe+ needs this loop :see-no-evil:
        for attempt in range(max_retries):
            try:
                chip_counter = self.asics.init(self.hardware.get_asic_frequency(), self.hardware.get_chip_count(), chips_enabled)
                print("Initialization successful.")
                break
            except Exception as e:
                logging.error("Attempt %d: Not enough chips found: %s", attempt + 1, e)

                # only retry on 1368s
                if not isinstance(self.asics, bm1366.BM1368):
                    raise

                if attempt < max_retries - 1:
                    time.sleep(1)  # Wait before the next attempt
                else:
                    logging.error("Max retries reached. Initialization failed.")
                    raise

        logging.info(f"{chip_counter} chips were found!")

        self.set_difficulty(512)
        self.extranonce2_interval = self.config[self.miner]["extranonce2_interval"]

        self.temp_thread = threading.Thread(target=self._monitor_temperature)
        self.temp_thread.start()

        self.receive_thread = threading.Thread(target=self._receive_thread)
        self.receive_thread.start()

        self.job_thread = threading.Thread(target=self._job_thread)
        self.job_thread.start()

        self.uptime_counter_thread = threading.Thread(target=self._uptime_counter_thread)
        self.uptime_counter_thread.start()

        self.led_thread = threading.Thread(target=self._led_thread)
        self.led_thread.start()

        influx_config = self.config.get('influx', None)
        self.influx = None
        if influx_config is not None and influx_config.get('enabled', False):
            stats_name = "mainnet_stats" if self.network == shared.BitcoinNetwork.MAINNET else \
                "testnet_stats" if self.network == shared.BitcoinNetwork.TESTNET else "regtest_stats"

            self.influx = influx.Influx(influx_config, self.stats, stats_name)
            try:
                self.influx.load_last_values()
            except Exception as e:
                logging.error("we really don't want to start without previous influx values: %s", e)
                self.hardware.shutdown()
                os._exit(0)

            # start writing thread after values were loaded
            self.influx.start()

        smartplug_config = self.config.get('smartplug', None)
        self.smartplug = None
        if smartplug_config is not None and smartplug_config.get('enabled', False):
            if not self.influx:
                logging.error("influx not enabled, skipping smartplug module")

            self.smartplug = smartplug.Tasmota(smartplug_config)
            self.influx.add_stats_callback(self.smartplug.add_smart_plug_energy_data)
            self.smartplug.start()

        alerter_config = self.config.get("alerter", None)
        self.alerter_thread = None
        if alerter_config is not None and alerter_config.get("enabled", False):
            if alerter_config["type"] == "discord-webhook":
                self.alerter = discord.DiscordWebhookAlerter(alerter_config)
                self.alerter_thread = threading.Thread(target=self._alerter_thread)
                self.alerter_thread.start()
            else:
                raise Exception(f"unknown alerter: {alerter_config['type']}")

        i2c_config = self.config.get("i2c_display", None)
        if i2c_config is not None and i2c_config.get("enabled", False):
            self.display_thread = threading.Thread(target=self._display_update)
            self.display_thread.start()

        rest_config = self.config.get("rest_api", None)
        if rest_config is not None and rest_config.get("enabled", False):
            self.rest_api = rest.ASICFrequencyManager(rest_config, self)
            self.rest_api.run()


    def _uptime_counter_thread(self):
        logging.info("uptime counter thread started ...")
        while not self.stop_event.is_set():
            with self.stats.lock:
                self.stats.total_uptime += 1
                self.stats.uptime += 1
            time.sleep(1)

        logging.info("uptime counter thread ended ...")

    def _alerter_thread(self):
        logging.info("Alerter thread started ...")
        self.alerter.alert("MINER", "started")
        while not self.stop_event.is_set():
            self.alerter.alert_if("NO_JOB", "no new job for more than 5 minutes!", (time.time() - self.last_job_time) > 5*60)
            self.alerter.alert_if("NO_RESPONSE", "no ASIC response for more than 5 minutes!", (time.time() - self.last_response) > 5*60)
            time.sleep(1)

        self.alerter.alert("MINER", "shutdown")
        logging.info("Alerter thread ended ...")

    def _display_update(self):
        logging.info("display update ...")
        self.display.init()
        while not self.stop_event.is_set():
                self.display.update()
                time.sleep(2)
        logging.info("display update ended ...")

    def _led_thread(self):
        logging.info("LED thread started ...")
        led_state = True
        while not self.stop_event.is_set():
            # if for more than 5 minutes no new job is received
            # we flash the light faster
            if time.time() - self.last_job_time > 5*60 or \
                time.time() - self.last_response > 5*60:
                led_state = not led_state
                self.hardware.set_led(led_state)
                time.sleep(0.25)
                continue

            # this gets triggered in 2s intervals
            # .wait() doesn't work reliably because it happens
            # that the submit method hangs forever and the
            # event wouldn't be fired then
            if self.led_event.is_set():
                self.led_event.clear()
                led_state = not led_state
                self.hardware.set_led(led_state)
                continue

            time.sleep(0.25)

        logging.info("LED thread ended ...")

    def _monitor_temperature(self):
        while not self.stop_event.is_set():

            temp = self.hardware.read_temperature_and_voltage()

            # trigger measurement of metrics
            if isinstance(self.asics, bm1366.BM1368):
                self.asics.request_temps()

            with self.stats.lock:
                self.stats.temp = temp["temp"][0]
                self.stats.temp2 = temp["temp"][1]
                self.stats.temp3 = temp["temp"][2]
                self.stats.temp4 = temp["temp"][3]
                self.stats.vdomain1 = temp["voltage"][0]
                self.stats.vdomain2 = temp["voltage"][1]
                self.stats.vdomain3 = temp["voltage"][2]
                self.stats.vdomain4 = temp["voltage"][3]

                # inject asic temps into the temp dict for display
                temp['asic_temp_raw'] = [
                    self.stats.asic_temp1_raw,
                    self.stats.asic_temp2_raw,
                    self.stats.asic_temp3_raw,
                    self.stats.asic_temp4_raw
                ]

            logging.info("temperature and voltage: %s", str(temp))



            for i in range(0, 4):
                if temp["temp"][i] is not None and temp["temp"][i] > 70.0:
                    logging.error("too hot, shutting down ...")
                    self.hardware.shutdown()
                    os._exit(1)

            time.sleep(1.5)

    def _serial_tx_func(self, data):
        with self.serial_lock:
            total_sent = 0
            while total_sent < len(data):
                sent = self.serial_port.write(data[total_sent:])
                if sent == 0:
                    raise RuntimeError("Serial connection broken")
                total_sent += sent
            if self.debug_bm1366:
                logging.debug("-> %s", bytearray(data).hex())

    def _serial_rx_func(self, size, timeout_ms):
        self.serial_port.timeout = timeout_ms / 1000.0

        data = self.serial_port.read(size)
        bytes_read = len(data)

        if self.debug_bm1366 and bytes_read > 0:
            logging.debug("serial_rx: %d", bytes_read)
            logging.debug("<- %s", data.hex())

        return data if bytes_read > 0 else None

    def cleanup_duplicate_finds(self):
        current_time = time.time()

        # clean up dict, delete old hashes, counts elements to pop from the list
        remove_first_n=0
        for timestamp, hash_key in self.found_timestamps:
            if current_time - timestamp > 600:
                #logging.debug(f"removing {hash_key} from found_hashes dict")
                if hash_key in self.found_hashes:
                    del self.found_hashes[hash_key]
                else:
                    pass
                    #logging.debug(f"{hash_key} not in dict")
                remove_first_n += 1
            else:
                break

        # pop elements
        #logging.debug(f"removing first {remove_first_n} element(s) of found_timestamps list")
        for i in range(0, remove_first_n):
            self.found_timestamps.pop(0)


    def hash_rate(self, time_period=600):
        current_time = time.time()
        total_work = 0

        #min_timestamp = current_time
        #max_timestamp = 0
        for shares, difficulty, timestamp in self.shares:
            # Consider shares only in the last 10 minutes
            if current_time - timestamp <= time_period:
                total_work += shares * (difficulty << 32)
                #min_timestamp = min(min_timestamp, timestamp)
                #max_timestamp = max(max_timestamp, timestamp)

        #if min_timestamp > max_timestamp:
        #    raise Exception("timestamp range calculation failed")

        #if min_timestamp == max_timestamp:
        #    return 0.0

        # Hash rate in H/s (Hashes per second)
        #hash_rate_hps = total_work / (max_timestamp - min_timestamp)
        hash_rate_hps = total_work / time_period

        # Convert hash rate to GH/s
        hash_rate_ghps = hash_rate_hps / 1e9
        logging.debug("\033[32mhash rate: %f GH/s\033[0m", hash_rate_ghps)
        return hash_rate_ghps

    def _set_target(self, target):
        self._target = '%064x' % target

    def set_difficulty(self, difficulty):
        # restrict to min 256
        difficulty = max(difficulty, 256)

        self._difficulty = difficulty
        self._set_target(shared.calculate_target(difficulty))
        self.asics.set_job_difficulty_mask(difficulty)

        with self.stats.lock:
            self.stats.difficulty = difficulty

    def set_submit_callback(self, cb):
        self.submit_cb = cb

    def accepted_callback(self):
        with self.stats.lock:
            self.stats.accepted += 1

    def not_accepted_callback(self):
        with self.stats.lock:
            self.stats.not_accepted += 1

    def _receive_thread(self):
        logging.info('receiving thread started ...')
        mask_nonce = 0x00000000
        mask_version = 0x00000000

        while not self.stop_event.is_set():
            byte = self._serial_rx_func(11, 100)

            if not byte:
                continue

            for i in range(0, len(byte)):
                self._buffer[self._write_index % 64] = byte[i]
                self._write_index += 1

            if self._write_index - self._read_index >= 11 and self._buffer[self._read_index % 64] == 0xaa and self._buffer[(self._read_index + 1) % 64] == 0x55:
                data = bytearray([0] * 11)
                for i in range(0, 11):
                    data[i] = self._buffer[self._read_index % 64]
                    self._read_index += 1

                #if self.debug_bm1366:
                #    logging.debug("<- %s", bytes(data).hex())

                asic_result = bm1366.AsicResult().from_bytes(bytes(data))
                if not asic_result or not asic_result.nonce:
                    continue

                # temperature response
                (temp_value, temp_id) = self.asics.try_get_temp_from_response(asic_result)
                if temp_value:
                    logging.debug(f"temp for chip {temp_id}: {temp_value}")

                    attribute_name = f"asic_temp{temp_id+1}_raw"
                    with self.stats.lock:
                        setattr(self.stats, attribute_name, temp_value)

                    continue

                with self.job_lock:
                    self.last_response = time.time()
                    result_job_id = self.asics.get_job_id_from_result(asic_result.job_id)
                    logging.debug("work received %02x", result_job_id)

                    if result_job_id not in self._jobs:
                        logging.debug("internal jobid %d not found", result_job_id)
                        continue

                    saved_job = self._jobs[result_job_id]
                    job = saved_job['job']
                    work = saved_job['work']
                    difficulty = saved_job['difficulty']

                    if result_job_id != work.id:
                        logging.error("mismatch ids")
                        continue

                    result = dict(
                        job_id = job._job_id,
                        extranonce2 = job._extranonce2, #shared.int_to_hex32(job._extranonce2),
                        ntime = job._ntime,
                        nonce = shared.int_to_hex32(asic_result.nonce),
                        version = shared.int_to_hex32(shared.reverse_uint16(asic_result.version) << 13),
                    )


                    is_valid, hash, zeros = shared.verify_work(difficulty, job, result)
                    network_target, network_zeros = shared.nbits_to_target(job._nbits)
                    pool_target, pool_zeros = shared.get_network_target(difficulty)

                    logging.debug("network-target: %s (%d)", network_target, network_zeros)
                    logging.debug("pool-target:    %s (%d)", pool_target, pool_zeros)
                    logging.debug("found hash:     %s (%d)", hash, zeros)

                    # detect duplicates
                    duplicate = hash in self.found_hashes

                    self.cleanup_duplicate_finds()

                    # save hash in dict
                    self.found_hashes[hash] = True
                    self.found_timestamps.append((time.time(), hash))

                    # some debug info
                    #logging.debug(f"{len(self.found_hashes)} in found_hashes dict, {len(self.found_timestamps)} in found_timestamps list")

                    if duplicate:
                        logging.warn("found duplicate hash!")

                    if hash < network_target:
                        logging.info("!!! it seems we found a block !!!")

                    # the hash isn't completly wrong but isn't lower than the target
                    # the asic uses power-of-two targets but the pool might not (eg ckpool)
                    # we should just pretend it didn't happen and not count it^^
                    if not is_valid and zeros >= pool_zeros:
                        logging.info("ignoring hash because higher than pool target")
                        continue


                    if is_valid:
                        mask_nonce |= asic_result.nonce
                        mask_version |= asic_result.version << 13

                        logging.debug(f"mask_nonce:   %s (%08x)", shared.int_to_bin32(mask_nonce, 4), mask_nonce)
                        logging.debug(f"mask_version: %s (%08x)", shared.int_to_bin32(mask_version, 4), mask_version)
                        x_nonce = (asic_result.nonce & 0x0000fc00) >> 10
                        logging.debug(f"result from asic {x_nonce}")

                    with self.stats.lock:
                        if hash < network_target:
                            self.stats.blocks_found += 1
                            self.stats.total_blocks_found += 1

                        if duplicate:
                            self.stats.duplicate_hashes += 1

                        self.stats.invalid_shares += 1 if not is_valid else 0
                        self.stats.valid_shares += 1 if is_valid else 0

                        # don't add to shares if it's invalid or it's a duplicate
                        if is_valid and not duplicate:
                            self.shares.append((1, difficulty, time.time()))

                        self.stats.hashing_speed = self.hash_rate()
                        hash_difficulty = shared.calculate_difficulty_from_hash(hash)
                        self.stats.best_difficulty = max(self.stats.best_difficulty, hash_difficulty)
                        self.stats.total_best_difficulty = max(self.stats.total_best_difficulty, hash_difficulty)

                    # restart miner with new extranonce2
                    #self.new_job_event.set() TODO

                # submit result without lock on the job!
                # we don't submit invalid hashes or duplicates
                if not is_valid or duplicate:
                    # if its invalid it would be rejected
                    # we don't try it but we can count it to not_accepted
                    self.not_accepted_callback()
                    logging.error("invalid result!")
                    continue


                logging.info("valid result")
                if not self.submit_cb:
                    logging.error("no submit callback set")
                elif not self.submit_cb(result):
                    self.stats.pool_errors += 1

        logging.info('receiving thread ended ...')



    def _job_thread(self):
        logging.info("job thread started ...")
        current_time = time.time()
        while not self.stop_event.is_set():
            self.new_job_event.wait(self.extranonce2_interval)
            self.new_job_event.clear()

            with self.job_lock:
                if not self.current_job:
                    logging.info("no job ...")
                    time.sleep(1)
                    continue

                extranonce2 = random.randint(0, 2**31-1)
                logging.debug("new extranonce2 %08x", extranonce2)
                self.current_job.set_extranonce2(extranonce2)

                self._internal_id += 1
                self._latest_work_id = self.asics.get_job_id(self._internal_id)

                work = bm1366.WorkRequest()
                logging.debug("new work %02x", self._latest_work_id)
                work.create_work(
                    self._latest_work_id,
                    0x00000000,
                    shared.hex_to_int(self.current_job._nbits),
                    shared.hex_to_int(self.current_job._ntime),
                    shared.reverse_bytes(shared.hex_to_bytes(self.current_job._merkle_root)),
                    shared.reverse_bytes(shared.hex_to_bytes(self.current_job._prevhash)),
                    shared.hex_to_int(self.current_job._version)
                )
                self.current_work = work

                # make deepcopies
                self._jobs[self._latest_work_id] = {
                    'job': copy.deepcopy(self.current_job),
                    'work': copy.deepcopy(self.current_work),
                    'difficulty': self._difficulty
                }

                self.led_event.set()

                self.asics.send_work(work)

        logging.info("job thread ended ...")

    def clean_jobs(self):
        with self.job_lock:
            logging.info("cleaning jobs ...")
            self._jobs = dict()
            self.current_job = None

    def start_job(self, job):
        logging.info("starting new job %s", job._job_id)

        self.last_job_time = time.time()
        with self.job_lock:
            self.current_job = job

            if self.verify_solo:
                try:
                    # only decode when verify_solo is enabled
                    coinb = job.deserialize_coinbase()
                    if coinb['height'] is not None:
                        logging.debug("mining for block %d", coinb['height'])

                    is_solo, value_our, value_total = shared.verify_solo(self.address, coinb)
                    logging.debug("solo mining verification passed! reward: %d", value_our)
                except Exception as e:
                    logging.error("verify_solo error: %s", e)
            else:
                logging.debug("solo mining not verified!")

            #logging.debug(json.dumps(job.deserialize_coinbase(), indent=4))


            self.new_job_event.set()
