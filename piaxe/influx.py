from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client import InfluxDBClient, Point, WriteOptions, WritePrecision
from datetime import datetime
import pytz
import logging
import threading
import json
import time

class Stats:
    def __init__(self):
        self.temp = 25.0
        self.temp2 = 25.0
        self.temp3 = 25.0
        self.temp4 = 25.0
        self.vdomain1 = 1200
        self.vdomain2 = 1200
        self.vdomain3 = 1200
        self.vdomain4 = 1200
        self.hashing_speed = 0.0
        self.invalid_shares = 0
        self.valid_shares = 0
        self.difficulty = 512
        self.best_difficulty = 0.0
        self.pool_errors = 0
        self.accepted = 0
        self.not_accepted = 0
        self.total_uptime = 0
        self.total_best_difficulty = 0.0
        self.uptime = 0
        self.blocks_found = 0
        self.total_blocks_found = 0
        self.duplicate_hashes = 0
        self.asic_temp1_raw = None
        self.asic_temp2_raw = None
        self.asic_temp3_raw = None
        self.asic_temp4_raw = None

        self.lock = threading.Lock()

    def import_dict(self, data):
        self.total_uptime = data.get('total_uptime', self.total_uptime)
        self.total_best_difficulty = data.get('total_best_difficulty', self.total_best_difficulty)
        self.total_blocks_found = data.get('total_blocks_found', self.total_blocks_found)

        logging.info("loaded total uptime: %s seconds", self.total_uptime)
        logging.info("loaded total best difficulty: %f.3", self.total_best_difficulty)
        logging.info("loaded total blocks found: %d", self.total_blocks_found)

class Influx:
    def __init__(self, config, stats, stats_name):
        # InfluxDB settings (replace with your own settings)
        self.host = config['host']
        self.port = config['port']
        self.token = config['token']
        self.org = config['org']
        self.bucket = config['bucket']
        self.stats_name = stats_name
        self.client = None
        self.tz = pytz.timezone(config['timezone'])
        self.stats = stats
        self.stop_event = threading.Event()
        self.callbacks = []
        self.connect()

    def add_stats_callback(self, callback):
        """Registers a callback function."""
        self.callbacks.append(callback)

    def start(self):
        self.submit_thread = threading.Thread(target=self._submit_thread)
        self.submit_thread.start()

    def shutdown(self):
        self.stop_event.set()
        self.submit_thread.join()

    def _submit_thread(self):
        while not self.stop_event.is_set():
            if not self.client:
                time.sleep(10)
                continue

            with self.stats.lock:
                point = Point(f"{ self.stats_name }").time(datetime.now(self.tz), WritePrecision.NS) \
                    .field("temperature", float(self.stats.temp or 0.0)) \
                    .field("temperature2", float(self.stats.temp2 or 0.0)) \
                    .field("temperature3", float(self.stats.temp3 or 0.0)) \
                    .field("temperature4", float(self.stats.temp4 or 0.0)) \
                    .field("vdomain1", float(self.stats.vdomain1 or 0.0)) \
                    .field("vdomain2", float(self.stats.vdomain2 or 0.0)) \
                    .field("vdomain3", float(self.stats.vdomain3 or 0.0)) \
                    .field("vdomain4", float(self.stats.vdomain4 or 0.0)) \
                    .field("hashing_speed", float(self.stats.hashing_speed)) \
                    .field("invalid_shares", int(self.stats.invalid_shares)) \
                    .field("valid_shares", int(self.stats.valid_shares)) \
                    .field("uptime", int(self.stats.uptime)) \
                    .field("best_difficulty", float(self.stats.best_difficulty)) \
                    .field("total_best_difficulty", float(self.stats.total_best_difficulty)) \
                    .field("pool_errors", int(self.stats.pool_errors)) \
                    .field("accepted", int(self.stats.accepted)) \
                    .field("not_accepted", int(self.stats.not_accepted)) \
                    .field("total_uptime", int(self.stats.total_uptime)) \
                    .field("total_blocks_found", int(self.stats.total_blocks_found)) \
                    .field("blocks_found", int(self.stats.blocks_found)) \
                    .field("difficulty", int(self.stats.difficulty)) \
                    .field("duplicate_hashes", int(self.stats.duplicate_hashes)) \
                    .field("asic_temp1_raw", int(self.stats.asic_temp1_raw or 0)) \
                    .field("asic_temp2_raw", int(self.stats.asic_temp2_raw or 0)) \
                    .field("asic_temp3_raw", int(self.stats.asic_temp3_raw or 0)) \
                    .field("asic_temp4_raw", int(self.stats.asic_temp4_raw or 0))

            for callback in self.callbacks:
                callback(point)

            try:
                write_api = self.client.write_api(write_options=SYNCHRONOUS)
                write_api.write(bucket=self.bucket, org=self.org, record=point)
                logging.debug("influx data written: %s", point.to_line_protocol())
            except Exception as e:
                logging.error("writing to influx failed: %s", e)

            time.sleep(15)

    def connect(self):
        # Connect to InfluxDB
        try:
            self.client = InfluxDBClient(url=f"http://{self.host}:{self.port}", token=self.token, org=self.org)
        except Exception as e:
            logging.error("connecting influx failed: %s", e)

    def bucket_exists(self, bucket_name):
        # List all buckets
        buckets = self.client.buckets_api().find_buckets().buckets

        # Check if the specified bucket is in the list
        for bucket in buckets:
            if bucket.name == bucket_name:
                return True
        return False

    def load_last_values(self):
        if not self.bucket_exists(self.bucket):
            logging.debug(f"Bucket {self.bucket} does not exist. Nothing imported.")
            return

        # Create a query to fetch the latest records
        query = f'''
            from(bucket: "{ self.bucket }")
            |> range(start: -1y)
            |> filter(fn: (r) => r["_measurement"] == "{ self.stats_name }")
            |> last()
        '''

        # Execute the query
        # better to let it raise an exception instead of resetting the total counters
        tables = self.client.query_api().query(query, org=self.org)


        # Process the results
        last_data = dict()
        for table in tables:
            for record in table.records:
                last_data[record.get_field()] = record.get_value()

        logging.debug("loaded values:\n"+json.dumps(last_data, indent=4))

        self.stats.import_dict(last_data)


    def close(self):
        # Close the connection
        if self.client:
            self.client.close()
