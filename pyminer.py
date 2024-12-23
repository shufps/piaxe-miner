# The MIT License (MIT)
#
# Copyright (c) 2014 Richard Moore
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import json, socket, sys, threading, time
from urllib import parse as urlparse
from shared import shared
#from cpu_miner import miner
from piaxe import miner
#import cpu_miner
import logging
import piaxe
import signal
import os
import datetime
import yaml

import threading, time, faulthandler, signal
import logging

def watchdog_thread():
    while True:
        time.sleep(60)
        with open('thread_dump.log', 'a') as f:
            faulthandler.dump_traceback(file=f)
            f.write("\n---\n")

watchdog = threading.Thread(target=watchdog_thread, daemon=True)
watchdog.start()


# Subscription state
class Subscription(object):
  '''Encapsulates the Subscription state from the JSON-RPC server'''

  _max_nonce = 0x7fffffff

  # Subclasses should override this
  def ProofOfWork(header):
    raise Exception('Do not use the Subscription class directly, subclass it')

  class StateException(Exception): pass

  def __init__(self):
    self._id = None
    self._difficulty = None
    self._extranonce1 = None
    self._extranonce2_size = None
    self._worker_name = None

    self._mining_thread = None

  # Accessors
  id = property(lambda s: s._id)
  worker_name = property(lambda s: s._worker_name)

  difficulty = property(lambda s: s._difficulty)

  extranonce1 = property(lambda s: s._extranonce1)
  extranonce2_size = property(lambda s: s._extranonce2_size)


  def set_worker_name(self, worker_name):
    if self._worker_name:
      raise self.StateException('Already authenticated as %r (requesting %r)' % (self._username, username))

    self._worker_name = worker_name


  def set_subscription(self, subscription_id, extranonce1, extranonce2_size):
    if self._id is not None:
      raise self.StateException('Already subscribed')

    self._id = subscription_id
    self._extranonce1 = extranonce1
    self._extranonce2_size = extranonce2_size


  def create_job(self, job_id, prevhash, coinb1, coinb2, merkle_branches, version, nbits, ntime):
    '''Creates a new Job object populated with all the goodness it needs to mine.'''

    if self._id is None:
      raise self.StateException('Not subscribed')

    return piaxe.miner.Job(
      job_id=job_id,
      prevhash=prevhash,
      coinb1=coinb1,
      coinb2=coinb2,
      merkle_branches=merkle_branches,
      version=version,
      nbits=nbits,
      ntime=ntime,
      extranonce1=self._extranonce1,
      extranonce2_size=self.extranonce2_size,
      max_nonce=self._max_nonce,
    )

  def __str__(self):
    return '<Subscription id=%s, extranonce1=%s, extranonce2_size=%d, difficulty=%d worker_name=%s>' % (self.id, self.extranonce1, self.extranonce2_size, self.difficulty, self.worker_name)

class SubscriptionSHA256D(Subscription):
  '''Subscription for Double-SHA256-based coins, like Bitcoin.'''

  ProofOfWork = shared.sha256d

class SimpleJsonRpcClient(object):
  '''Simple JSON-RPC client.

    To use this class:
      1) Create a sub-class
      2) Override handle_reply(self, request, reply)
      3) Call connect(socket)

    Use self.send(method, params) to send JSON-RPC commands to the server.

    A new thread is created for listening to the connection; so calls to handle_reply
    are synchronized. It is safe to call send from withing handle_reply.
  '''

  class ClientException(Exception): pass

  class RequestReplyException(Exception):
    def __init__(self, message, reply, request = None):
      Exception.__init__(self, message)
      self._reply = reply
      self._request = request

    request = property(lambda s: s._request)
    reply = property(lambda s: s._reply)

  class RequestReplyWarning(RequestReplyException):
    '''Sub-classes can raise this to inform the user of JSON-RPC server issues.'''
    pass

  def __init__(self):
    self._socket = None
    self._lock = threading.RLock()
    self._rpc_thread = None
    self._message_id = 1
    self._requests = dict()
    self.error_event = threading.Event()

  def stop(self):
    self.error_event.set()

    try:
        if self._socket:
          self._socket.shutdown(socket.SHUT_RDWR)
          self._socket.close()
    except OSError as e:
        print(f"Error when closing socket: {e}")

    if self._rpc_thread:
      logging.debug("joining rpc_thread")
      self._rpc_thread.join()
      logging.debug("joining done")
      self._rpc_thread = None

  def _handle_incoming_rpc(self):
    data = ""
    while not self.error_event.is_set():
      try:
        # Get the next line if we have one, otherwise, read and block
        if '\n' in data:
          (line, data) = data.split('\n', 1)
        else:
          try:
            chunk = self._socket.recv(1024)
            if not chunk:
              raise Exception("tcp connection closed ...")
            chunk = chunk.decode('utf-8')
            data += chunk
            continue
          except socket.timeout:
            # we have to handle it but we don't care actually
            continue

        if log_protocol:
          logging.debug('JSON-RPC Server > ' + line)

        # Parse the JSON
        try:
          reply = json.loads(line)
        except Exception as e:
          logging.error("JSON-RPC Error: Failed to parse JSON %r (skipping)" % line)
          continue

        try:
          request = None
          with self._lock:
            if 'id' in reply and reply['id'] in self._requests:
              request = self._requests[reply['id']]
            self.handle_reply(request = request, reply = reply)
        except self.RequestReplyWarning as e:
          output = str(e)
          if e.request:
            try:
              output += '\n  ' + e.request
            except TypeError:
              output += '\n  ' + str(e.request)
          output += '\n  ' + str(e.reply)
          logging.error(output)
      except Exception as e:
        logging.error('Exception in RPC thread: %s' % str(e))
        self.error_event.set()
    logging.error("error flag set ... ending handle_incoming_rpc thread")


  def handle_reply(self, request, reply):
    # Override this method in sub-classes to handle a message from the server
    raise self.RequestReplyWarning('Override this method')

  def _send_message(self, message):
      ''' Internal method to send a message '''
      try:
          self._socket.send((message + '\n').encode('utf-8'))
          logging.debug("send successful")
      except Exception as e:
          logging.error("send failed: %s", e)
          self.error_event.set()


  def send(self, method, params, timeout=10):
      '''Sends a message to the JSON-RPC server with a timeout'''

      if not self._socket:
          raise self.ClientException('Not connected')

      request = dict(id=self._message_id, method=method, params=params)
      message = json.dumps(request)

      with self._lock:
          self._requests[self._message_id] = request
          self._message_id += 1

      # Create a thread to send the message
      sender_thread = threading.Thread(target=self._send_message, args=(message,))
      sender_thread.start()
      sender_thread.join(timeout)

      if sender_thread.is_alive():
          # Handle the timeout situation
          logging.error("Timeout occurred in send method")
          self.error_event.set()
          return False

      # If here, the send operation completed within the timeout
      if log_protocol:
          logging.debug('JSON-RPC Server < ' + message)

      return True



  def mining_submit(self, result):
    params = [ self._subscription.worker_name ] + [ result[k] for k in ('job_id', 'extranonce2', 'ntime', 'nonce', 'version') ]
    try:
      ret = self.send(method = 'mining.submit', params = params)
      if not ret:
        raise Exception("mining.submit failed")
      return True
    except Exception as e:
      logging.error("mining.submit exception: %s", e)
    return False

  def connect(self, socket):
    '''Connects to a remote JSON-RPC server'''

    if self._rpc_thread:
      raise self.ClientException('Already connected')

    self._socket = socket
    # submit sometimes would hang forever
    self._socket.settimeout(10)

    self._rpc_thread = threading.Thread(target = self._handle_incoming_rpc)
    self._rpc_thread.daemon = True
    self._rpc_thread.start()


# Miner client
class Miner(SimpleJsonRpcClient):
  '''Simple mining client'''

  class MinerWarning(SimpleJsonRpcClient.RequestReplyWarning):
    def __init__(self, message, reply, request = None):
      SimpleJsonRpcClient.RequestReplyWarning.__init__(self, 'Mining Sate Error: ' + message, reply, request)

  class MinerAuthenticationException(SimpleJsonRpcClient.RequestReplyException): pass

  def __init__(self, url, username, password, miner, suggest_difficulty):
    SimpleJsonRpcClient.__init__(self)

    self._url = url
    self._username = username
    self._password = password
    self._suggest_difficulty = suggest_difficulty

    self._subscription = SubscriptionSHA256D()

    self._job = None

    self._miner = miner
    self._miner.set_submit_callback(self.mining_submit)

    self._accepted_shares = 0

  # Accessors
  url = property(lambda s: s._url)
  username = property(lambda s: s._username)
  password = property(lambda s: s._password)


  # Overridden from SimpleJsonRpcClient
  def handle_reply(self, request, reply):

    # New work, stop what we were doing before, and start on this.
    if reply.get('method') == 'mining.notify':
      if 'params' not in reply or len(reply['params']) != 9:
        raise self.MinerWarning('Malformed mining.notify message', reply)

      (job_id, prevhash, coinb1, coinb2, merkle_branches, version, nbits, ntime, clean_jobs) = reply['params']

      # Create the new job
      self._job = self._subscription.create_job(
        job_id = job_id,
        prevhash = prevhash,
        coinb1 = coinb1,
        coinb2 = coinb2,
        merkle_branches = merkle_branches,
        version = version,
        nbits = nbits,
        ntime = ntime
      )
      if clean_jobs:
        self._miner.clean_jobs()

      self._miner.start_job(self._job)

      logging.debug('New job: job_id=%s' % job_id)

    # The server wants us to change our difficulty (on all *future* work)
    elif reply.get('method') == 'mining.set_difficulty':
      if 'params' not in reply or len(reply['params']) != 1:
        raise self.MinerWarning('Malformed mining.set_difficulty message', reply)

      (difficulty, ) = reply['params']
      self._miner.set_difficulty(int(difficulty))

      logging.debug('Change difficulty: difficulty=%s' % difficulty)

    # This is a reply to...
    elif request:

      # ...subscribe; set-up the work and request authorization
      if request.get('method') == 'mining.subscribe':
        if 'result' not in reply or len(reply['result']) != 3:
          raise self.MinerWarning('Reply to mining.subscribe is malformed', reply, request)

        (tmp, extranonce1, extranonce2_size) = reply['result']

        if not isinstance(tmp, list) or len(tmp) < 1 or not isinstance(tmp[0], list) or not len(tmp[0]) == 2:
          raise self.MinerWarning('Reply to mining.subscribe is malformed', reply, request)

        notify_subscription_id = None
        for subscription in tmp:
          if subscription[0] == "mining.notify":
            notify_subscription_id = subscription[1]

        if notify_subscription_id == None:
          raise self.MinerWarning('Reply to mining.subscribe is malformed', reply, request)

        self._subscription.set_subscription(notify_subscription_id, extranonce1, extranonce2_size)

        logging.debug('Subscribed: subscription_id=%s' % notify_subscription_id)

        # Request authentication
        self.send(method = 'mining.authorize', params = [ self.username, self.password ])

      # ...authorize; if we failed to authorize, quit
      elif request.get('method') == 'mining.authorize':
        if 'result' not in reply or not reply['result']:
          raise self.MinerAuthenticationException('Failed to authenticate worker', reply, request)

        worker_name = request['params'][0]
        self._subscription.set_worker_name(worker_name)

        logging.debug('Authorized: worker_name=%s' % worker_name)

        # suggest_difficulty after authorize
        if self._suggest_difficulty:
            try:
              self.send(method = 'mining.suggest_difficulty', params = [self._suggest_difficulty])
            except:
              logging.warn("suggest_difficulty failed, mybe not supported by the stratum server")

      # ...submit; complain if the server didn't accept our submission
      elif request.get('method') == 'mining.submit':
        if 'result' not in reply or not reply['result']:
          logging.info('Share - Invalid')
          self._miner.not_accepted_callback()
          raise self.MinerWarning('Failed to accept submit', reply, request)

        self._accepted_shares += 1
        self._miner.accepted_callback()
        logging.info('Accepted shares: %d' % self._accepted_shares)

      # ??? *shrug*
      else:
        raise self.MinerWarning('Unhandled message', reply, request)

    # ??? *double shrug*
    else:
      raise self.MinerWarning('Bad message state', reply)

  def serve(self):
    '''Begins the miner. This method does not return.'''

    # Figure out the hostname and port
    url = urlparse.urlparse(self.url)
    hostname = url.hostname or ''
    port = url.port or 9333

    logging.info('Starting server on %s:%d' % (hostname, port))
    # clear error if there was any
    self.error_event.clear()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((hostname, port))
    self.connect(sock)

    self.send(method = 'mining.subscribe', params = [ f"{self._miner.get_user_agent()}" ])

  def shutdown(self):
    self._miner.shutdown()



def setup_logging(log_level, log_filename):
    # Create a logger
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Create a formatter
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Create a handler for logging to the console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # If a log filename is provided, also log to a file
    if log_filename:
        file_handler = logging.FileHandler(log_filename, mode='w')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


# make it accessible to the sigint handler
pyminer = None

def sigint_handler(signal_received, frame):
    print('SIGINT (Ctrl+C) captured, exiting gracefully')
    if pyminer is not None:
      pyminer.shutdown()
    os._exit(0)

def handle_exception(exc_type, exc_value, exc_traceback):
    """
    Custom exception handler.
    Logs the exception information before the program exits.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        # For user interrupts, log at a lower level and do not include traceback
        logging.warning("Application interrupted by user.")
    else:
        # Log all other exceptions with traceback
        logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


# CLI for cpu mining
if __name__ == '__main__':
  import argparse

  # Parse the command line
  parser = argparse.ArgumentParser(description="PyMiner is a Stratum CPU mining client. "
                                               "If you like this piece of software, please "
                                               "consider supporting its future development via "
                                               "donating to one of the addresses indicated in the "
                                               "README.md file")

  parser.add_argument('-o', '--url', help = 'stratum mining server url (eg: stratum+tcp://foobar.com:3333)')
  parser.add_argument('-u', '--user', dest = 'username', default = '', help = 'username for mining server', metavar = "USERNAME")
  parser.add_argument('-p', '--pass', dest = 'password', default = '', help = 'password for mining server', metavar = "PASSWORD")

  parser.add_argument('-O', '--userpass', help = 'username:password pair for mining server', metavar = "USERNAME:PASSWORD")

  parser.add_argument('-B', '--background', action ='store_true', help = 'run in the background as a daemon')

  parser.add_argument('-q', '--quiet', action ='store_true', help = 'suppress non-errors')
  parser.add_argument('-P', '--dump-protocol', dest = 'protocol', action ='store_true', help = 'show all JSON-RPC chatter')
  parser.add_argument('-d', '--debug', action ='store_true', help = 'show extra debug information')

  parser.add_argument('-l', '--log-file', dest = 'logfile', default='', help = 'log to file')

  parser.add_argument('-c', '--config', dest = 'config', default='config.yml', help="use configfile")

  options = parser.parse_args(sys.argv[1:])

  message = None

  # Get the username/password
  username = options.username
  password = options.password

  if options.userpass:
    if username or password:
      message = 'May not use -O/-userpass in conjunction with -u/--user or -p/--pass'
    else:
      try:
        (username, password) = options.userpass.split(':')
      except Exception as e:
        message = 'Could not parse username:password for -O/--userpass'

  # Was there an issue? Show the help screen and exit.
  if message:
    parser.print_help()
    print()
    print(message)
    sys.exit(1)

  log_level = logging.INFO

  if options.debug:
    log_level = logging.DEBUG

  setup_logging(log_level, options.logfile)

  # write all exceptions to log file
  sys.excepthook = handle_exception

  log_protocol = options.protocol

  # The want a daemon, give them a daemon
  if options.background:
    import os
    if os.fork() or os.fork(): sys.exit()

  signal.signal(signal.SIGINT, sigint_handler)

  username_parts = options.username.split(".")
  address = username_parts[0]

  network = shared.detect_btc_network(address)
  if network == shared.BitcoinNetwork.UNKNOWN:
    logging.error("unknown address type: %s", address)

  # Load configuration from YAML
  with open(options.config, 'r') as file:
      config = yaml.safe_load(file)

  suggest_difficulty = config.get('suggest_difficulty', None)

  piaxeMiner = miner.BM1366Miner(config, address, network)
  piaxeMiner.init()

  # Heigh-ho, heigh-ho, it's off to work we go...

  while True:
    try:
      pyminer = Miner(options.url, username, password, piaxeMiner, suggest_difficulty)
      pyminer.serve()
    except Exception as e:
      logging.error("exception in serve ... restarting client")
      pyminer.error_event.set()

    logging.debug("waiting for error")
    pyminer.error_event.wait()
    logging.debug("error received")
    pyminer.stop()
    time.sleep(5)


