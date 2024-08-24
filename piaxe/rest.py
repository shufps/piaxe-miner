try:
    from flask import Flask, request, jsonify, send_from_directory
except:
    pass
import threading
import logging
import random

class RestAPI:
    def __init__(self, config, miner, stats):
        self.miner = miner
        self.hardware = miner.hardware
        self.asics = miner.asics
        self.app = Flask(__name__)
        self.cm = self.asics.clock_manager
        self.config = config
        self.stats = stats

        # Define routes
        self.app.add_url_rule('/clocks', 'get', self.get_clocks, methods=['GET'])
        self.app.add_url_rule('/clock/<int:id>', 'set', self.set_clock, methods=['POST'])
        self.app.add_url_rule('/stats', 'get_stats', self.get_stats, methods=['GET'])
        self.app.add_url_rule('/pwm/<int:id>/set', 'set_pwm', self.set_pwm, methods=['POST'])  # Updated to accept variable PWM ID
        self.app.add_url_rule('/influx/stats', 'get_influx_stats', self.get_influx_stats, methods=['GET'])


        # Route to serve the index.html
        self.app.add_url_rule('/', 'root', self.root)

    def root(self):
        # Serve index.html
        return send_from_directory("./manager", 'index.html')

    def get_clocks(self):
        clocks = self.cm.get_clock(-1)
        return jsonify(clocks)

    def get_stats(self):
        # Dummy data for example purposes:
        stats = {
            "hashrates": [random.randint(50, 100) for _ in range(16)],  # Random hash rates for 16 ASICs
            "voltages": [random.uniform(1.0, 1.5) for _ in range(4)]  # Random voltages for 4 domains
        }
        return jsonify(stats)

    def set_clock(self, id):
        if id < 0 or id >= self.cm.num_asics:
            return jsonify({"error": "Invalid ASIC ID"}), 400
        new_frequency = float(request.json.get('frequency'))
        if new_frequency is None or not (50.0 <= new_frequency <= 550.0):
            return jsonify({"error": f"Invalid frequency {new_frequency}"}), 400
        try:
            self.cm.set_clock(id, new_frequency)
        except Exception as e:
            logging.error(e)
            return jsonify({"error": f"Error setting clock to {new_frequency}"}), 400
        return jsonify({"success": True, "frequency": new_frequency})

    def set_pwm(self, id):
        pwm_value = float(request.json.get('pwm_value'))
        if 0.0 <= pwm_value <= 1.0:
            try:
                self.hardware.set_fan_speed(id-1, pwm_value)
                return jsonify({"success": True, "pwm_value": pwm_value, "channel_id": id})
            except Exception as e:
                logging.error(e)
                return jsonify({"error": f"Error setting PWM value for channel {id}"}), 400
        else:
            return jsonify({"error": f"Invalid PWM value for channel {id}. Must be between 0.0 and 1.0"}), 400

    # InfluxDB Stats Endpoint
    def get_influx_stats(self):
        with self.stats.lock:
            stats = {
                "temperature": self.stats.temp,
                "temperature2": self.stats.temp2,
                "temperature3": self.stats.temp3,
                "temperature4": self.stats.temp4,
                "vdomain1": self.stats.vdomain1,
                "vdomain2": self.stats.vdomain2,
                "vdomain3": self.stats.vdomain3,
                "vdomain4": self.stats.vdomain4,
                "hashing_speed": self.stats.hashing_speed,
                "invalid_shares": self.stats.invalid_shares,
                "valid_shares": self.stats.valid_shares,
                "uptime": self.stats.uptime,
                "best_difficulty": self.stats.best_difficulty,
                "total_best_difficulty": self.stats.total_best_difficulty,
                "pool_errors": self.stats.pool_errors,
                "accepted": self.stats.accepted,
                "not_accepted": self.stats.not_accepted,
                "total_uptime": self.stats.total_uptime,
                "total_blocks_found": self.stats.total_blocks_found,
                "blocks_found": self.stats.blocks_found,
                "difficulty": self.stats.difficulty,
                "duplicate_hashes": self.stats.duplicate_hashes
            }
        return jsonify(stats)

    def run(self):
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", "5000"))
        def run_app():
            self.app.run(host=host, port=port, debug=True, use_reloader=False)

        self.server_thread = threading.Thread(target=run_app)
        self.server_thread.start()
