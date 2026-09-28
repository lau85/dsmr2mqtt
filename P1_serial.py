"""
Read dsmr telgrams from P1 USB serial.


To test in bash the P1 usb connector:
raw -echo < /dev/ttyUSB0; cat -vt /dev/ttyUSB0

OR
sudo apt-get install -y python3-serial
sudo chmod o+rw /dev/ttyUSB0
python3 -m serial.tools.miniterm /dev/ttyUSB0 115200 --xonxoff



        This program is free software: you can redistribute it and/or modify
        it under the terms of the GNU General Public License as published by
        the Free Software Foundation, either version 3 of the License, or
        (at your option) any later version.

        This program is distributed in the hope that it will be useful,
        but WITHOUT ANY WARRANTY; without even the implied warranty of
        MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
        GNU General Public License for more details.

        You should have received a copy of the GNU General Public License
        along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import serial
import threading
import time
import re

from datetime import datetime, timedelta

import config as cfg

# Detecting and repairing a telegram damaged by mains noise (see the module
# docstring in dsmr_repair.py for the full approach) is a distinct concern
# from reading the serial port, so it lives in its own file.
from dsmr_repair import verify_crc, decode_lines, TelegramRepairer

# Logging
import __main__
import logging
import os
script = os.path.basename(__main__.__file__)
script = os.path.splitext(script)[0]
logger = logging.getLogger(script + "." + __name__)


# Telegrams that fail CRC and can't be repaired are dumped here (raw bytes, exactly
# as received) for later offline analysis -- eg to spot new corruption patterns
# worth adding a repair for. On tmpfs (RAM-backed), so this costs no SD card wear
# and is wiped on reboot; capped to the most recent N files so sustained noise
# can't fill the (also shared) ramdisk mount. Disabled by default to save space;
# set _SAVE_FAILED_TELEGRAMS to True to collect them again.
_SAVE_FAILED_TELEGRAMS = False
_FAILED_TELEGRAM_DIR = "/mnt/ramdisk/dsmr_failed_telegrams"
_MAX_FAILED_TELEGRAMS = 500

# A telegram from this meter is normally ~1.1KB. The end-of-telegram line ("!XXXX")
# is recognized only by its leading "!"; if noise corrupts that one byte, the read
# loop won't see a terminator and will keep reading straight into the *next*
# telegram, merging them (and, under sustained heavy noise, potentially several
# more after that, each with its own chance of a corrupted terminator). This caps
# a single telegram read attempt so a run of bad luck can't make it grow
# unbounded -- costly since every extra byte adds to the CRC/repair-search work
# below on what is, either way, going to be discarded.
_MAX_TELEGRAM_BYTES = 1600


class TaskReadSerial(threading.Thread):

  def __init__(self, trigger, stopper, telegram):
    """

    Args:
      :param threading.Event() trigger: signals that new telegram is available
      :param threading.Event() stopper: stops thread
      :param list() telegram: dsmr telegram
    """

    logger.debug(">>")
    super().__init__()
    self.__trigger = trigger
    self.__stopper = stopper
    self.__telegram = telegram
    self.__counter = 0
    self.__rxbuf = bytearray()

    self.__start_time = None
    self.__start_exported = None
    self.__last_average = None
    self.__last_period_end_average = None
    self.__last_state_time = None
    self.__state_time_corrections_count = 0
    self.__telegram_count = 0
    self.__corrupt_count = 0
    self.__repaired_count = 0
    self.__repairer = TelegramRepairer()

    if _SAVE_FAILED_TELEGRAMS:
      try:
        os.makedirs(_FAILED_TELEGRAM_DIR, exist_ok=True)
      except Exception:
        logger.exception(f"Could not create {_FAILED_TELEGRAM_DIR}; "
                          f"failed telegrams won't be saved for analysis")

    # [ Serial parameters ]
    if cfg.PRODUCTION:
      self.__tty = serial.Serial()
      self.__tty.port = cfg.ser_port
      self.__tty.baudrate = cfg.ser_baudrate
      self.__tty.bytesize = serial.SEVENBITS
      self.__tty.parity = serial.PARITY_EVEN
      self.__tty.stopbits = serial.STOPBITS_ONE
      self.__tty.xonxoff = 0
      self.__tty.rtscts = 0
      self.__tty.timeout = 20
      logger.info(f"Using USB device {self.__tty.port} with baud {self.__tty.baudrate}")

    try:
      if cfg.PRODUCTION:
        self.__tty.open()
        logger.debug(f"serial {self.__tty.port} opened")
      else:
        self.__tty = open(cfg.SIMULATORFILE, 'rb')

    except Exception as e:
      logger.error(f"ReadSerial: {type(e).__name__}: {str(e)}")
      self.__stopper.set()
      raise ValueError('Cannot open P1 serial port', cfg.ser_port)

  def __del__(self):
    logger.debug(">>")

  def __preprocess(self):

    state_time = self.__read_state_time()
    current_exported = None
    current_export_power = None
    for element in self.__telegram:
      try:
        value = re.match(r"1-0:2\.8\.0\((\d{6}\.\d{3})\*kWh\)", element).group(1)
        current_exported = float(value)
      except AttributeError:
        pass

      try:
        value = re.match(r"1-0:2\.7\.0\((\d{2}\.\d{3})\*kW\)", element).group(1)
        current_export_power = float(value)
      except AttributeError:
        pass

    if state_time is None or current_exported is None:
      return

    #First run, when start_time and start_exported is not set
    if self.__start_time is None:
      self.__start_time = state_time
      self.__start_exported = current_exported

    cycle_duration = state_time.timestamp() - self.__start_time.timestamp()
    cycle_exported = current_exported - self.__start_exported

    if cycle_duration > 30:
        average = cycle_exported * 3600 / cycle_duration
    else:
        if current_export_power is not None:
            average = current_export_power
        else:
            average = self.__last_average

    # To make first minute of average value graph look smoother when instant power is changing fast
    if cycle_duration < 60 and self.__last_average is not None:
        average = (average + self.__last_average * 9) / 10


    # Insert the virtual entries in the dsmr telegram
    if average is not None and average >= 0 and average < 20:
        line = f"1-0:2.7.9({average:06.3f}*kW)"
        self.__telegram.append(line)
        self.__last_average = average

    if (state_time.minute % 15 == 0 and state_time.minute != self.__start_time.minute) or cycle_duration > 900:
        self.__start_time = state_time
        self.__start_exported = current_exported
        self.__last_period_end_average = self.__last_average

    if self.__last_period_end_average is not None:
        self.__telegram.append(f"1-0:2.7.8({self.__last_period_end_average:06.3f}*kW)")

  def __read_state_time(self):
    state_time = None
    date_text = ""
    for element in self.__telegram:
      try:
        date_text = re.match(r"0-0:1\.0\.0\((\d{12})\*?[SW]\)", element).group(1)
      except AttributeError:
        pass

    try:
      state_time = datetime.strptime(date_text, "%y%m%d%H%M%S")
    except ValueError as e:
      logging.error(f"Failed to parse datetime from value '{date_text}'")

    try:
      if self.__last_state_time is not None:
        if state_time is None:
          state_time = self.__last_state_time + timedelta(seconds = 1)
          logging.error(f"Fixing state_time to '{state_time}'")
        else:
          delta = (state_time - self.__last_state_time).total_seconds()
          if delta < 0 or delta > 60:
            # only allow to do time correction 3 times, then taking timestamp from message.
            if self.__state_time_corrections_count < 3:
              state_time = self.__last_state_time + timedelta(seconds = 1)
              logging.error(f"Fixing state_time to '{state_time}'")
              self.__state_time_corrections_count += 1
            else:
              self.__state_time_corrections_count = 0
          else:
            self.__state_time_corrections_count = 0
    except Exception:
      logger.exception("Error in __read_state_time")

    self.__last_state_time = state_time

    return state_time

  def __next_line(self):
    """
      Returns the next line from the P1 port, decoded and without trailing CR LF.

      pyserial's readline() reads the port one byte at a time (a select + read
      syscall and timeout bookkeeping per byte), which is very CPU expensive on
      a Raspberry Pi 1. Instead read whatever is already waiting in the driver
      buffer in one go and split the lines here.
      On a read timeout the partial line (possibly empty) is returned, like readline().
      In non-production mode, lines are read from the simulator file.

    Returns:
      (str, bytes): decoded line, and the exact raw bytes it was decoded from
      (including the CR LF terminator, if any); the raw bytes are used for
      DSMR checksum validation in __read_serial().
    """
    if not cfg.PRODUCTION:
      raw = self.__tty.readline()
      return raw.decode('utf-8', errors='replace').rstrip(), raw

    while True:
      idx = self.__rxbuf.find(b'\n')
      if idx >= 0:
        raw = bytes(self.__rxbuf[:idx + 1])
        del self.__rxbuf[:idx + 1]
        return raw.decode('utf-8', errors='replace').rstrip(), raw

      # Blocks (up to the port timeout) until at least 1 byte is available
      chunk = self.__tty.read(self.__tty.in_waiting or 1)
      if not chunk:
        raw = bytes(self.__rxbuf)
        self.__rxbuf.clear()
        return raw.decode('utf-8', errors='replace').rstrip(), raw
      self.__rxbuf += chunk

  def __sync_to_header(self):
    """
      Reads (and discards) lines until one starts with "/", the telegram header
      marker. Every read attempt starts here rather than with a plain
      __next_line(), so it always begins aligned on a real telegram boundary --
      otherwise, once a previous attempt is abandoned mid-telegram (size cap or
      an unrepairable checksum), nothing would otherwise force the byte stream
      back into phase: with fixed-length, repeating-ish telegram content, a
      lucky '!'-shaped line can keep matching just enough to make every
      following attempt fail too, indefinitely, rather than recovering after
      one bad telegram as intended.

    Returns:
      (str, bytes): the header line and its raw bytes, as __next_line() returns
    """
    line, raw = self.__next_line()
    while (not self.__stopper.is_set()) and (not line.startswith('/')):
      if (not cfg.PRODUCTION) and line.startswith('EOF'):
        self.__stopper.set()
        break
      line, raw = self.__next_line()
    return line, raw

  def __save_failed_telegram(self, raw_telegram, suffix="", checksum_line=None):
    """
      Dumps a telegram that failed CRC and couldn't be repaired to
      _FAILED_TELEGRAM_DIR for later offline analysis, then prunes that
      directory down to the _MAX_FAILED_TELEGRAMS most recent files. Does
      nothing unless _SAVE_FAILED_TELEGRAMS is set. Never raises -- a failure
      here must not interrupt telegram processing.

      CR bytes are stripped from the dump (DSMR lines are CR LF terminated) so
      it reads cleanly with plain `cat`/an editor instead of showing "^M" at
      the end of every line; the corruption itself -- the interesting part --
      lives in the other bytes and is unaffected.

    Args:
      :param str suffix: appended to the filename (eg "_leadin") so multiple
        dumps for the same telegram number/second -- eg the unrecoverable
        lead-in discarded by __try_recover_second_telegram(), saved alongside
        a later, unrelated save under the same counter -- don't overwrite
        each other.
      :param str checksum_line: the "!XXXX" that raw_telegram failed to match,
        appended (hex digits only, raw_telegram already ends in "!") so the
        dump is a complete, independently re-verifiable telegram -- without
        it, nothing recorded here could later be confirmed as a correct fix
        rather than just a plausible-looking guess. Omitted for a telegram
        that never reached a checksum line at all (eg _MAX_TELEGRAM_BYTES).
    """
    if not _SAVE_FAILED_TELEGRAMS:
      return

    try:
      name = f"{time.strftime('%Y%m%d-%H%M%S')}_{self.__counter:06d}{suffix}.raw"
      data = bytes(raw_telegram)
      if checksum_line:
        match = re.match(r"!([0-9A-Fa-f]{4})", checksum_line)
        if match:
          data += match.group(1).encode()
      with open(os.path.join(_FAILED_TELEGRAM_DIR, name), 'wb') as f:
        f.write(data.replace(b'\r', b''))

      entries = sorted(os.scandir(_FAILED_TELEGRAM_DIR), key=lambda e: e.name)
      for stale in entries[:-_MAX_FAILED_TELEGRAMS]:
        os.remove(stale.path)
    except Exception:
      logger.exception(f"Error saving failed telegram #{self.__counter} for analysis")

  def __try_recover_second_telegram(self, raw_telegram):
    """
      When a read attempt goes oversized (2+ telegrams merged after a
      corrupted terminator, see _MAX_TELEGRAM_BYTES), looks for an embedded
      telegram header elsewhere in what was read -- a real, later telegram
      whose start we blew past without recognizing it, since only its own
      terminator, not its header, is what a mid-telegram read attempt would
      normally look for. If found, the unrecoverable lead-in before it is
      saved for analysis and discarded, and the embedded telegram is read to
      completion on its own, so a single corrupted terminator costs at most
      the telegram in front of it, not the one after it too.

    Args:
      :param bytearray raw_telegram: the oversized attempt's raw bytes so far

    Returns:
      (bytearray, str) | None: the embedded telegram's raw bytes (through its
      own "!") and checksum line, ready for the normal CRC/repair path below;
      or None if no embedded header was found, or it also went oversized.
    """
    # The header is fixed for a given meter -- this attempt's own leading line
    # is a known-good copy of it -- so search for another occurrence of those
    # exact bytes anywhere later in the buffer, rather than requiring a clean
    # newline right before it: corruption severe enough to cause a merge often
    # smears that boundary too (silently defeating a line-start-only search),
    # while the header text itself, being further along, often survives intact.
    own_header = bytes(raw_telegram).split(b'\r', 1)[0].split(b'\n', 1)[0]
    split = bytes(raw_telegram).find(own_header, 1) if own_header else -1
    if split < 0:
      return None

    logger.warning(f"Telegram #{self.__counter}: found an embedded header at byte "
                    f"{split} of the oversized read; discarding the lead-in and "
                    f"recovering the telegram after it")
    self.__save_failed_telegram(raw_telegram[:split], suffix="_leadin")

    # The embedded telegram's bytes already in hand are all header/body lines:
    # too little of it fit before the size cap for its own "!" to already be
    # among them, or this read wouldn't have gone oversized. Keep reading
    # fresh lines -- exactly like a normal read attempt -- until this
    # telegram's own checksum line, or give up if it's oversized too.
    raw2 = bytearray(raw_telegram[split:])
    line, raw = self.__next_line()
    while (not self.__stopper.is_set()) and (not line.startswith('!')):
      raw2 += raw

      if len(raw2) > _MAX_TELEGRAM_BYTES:
        return None

      line, raw = self.__next_line()

    if self.__stopper.is_set():
      return None

    bang_idx = raw.find(b'!')
    if bang_idx >= 0:
      raw2 += raw[:bang_idx + 1]
    return raw2, line

  def __read_serial(self):
    """
      Opens & Closes serial port
      Reads dsmr telegrams; stores in global variable (self.__telegram)
      Sets threading event to signal other clients (parser) that
      new telegram is available.
      In non-production mode, reads telegrams from file

    Returns:
      None
    """
    logger.debug(">>")

    while not self.__stopper.is_set():

      # wait till parser has copied telegram content
      # ...we need the opposite of trigger.wait()...block when set; not available
      while self.__trigger.is_set():
        time.sleep(0.01)

      # Find the next telegram header before starting to read one, so a read
      # attempt always begins aligned on a real telegram boundary (see
      # __sync_to_header()).
      line, raw = self.__sync_to_header()
      if self.__stopper.is_set():
        break

      self.__counter += 1

      # Raw bytes exactly as received. The checksum is validated on these, every
      # repair is made on these, and the decoded lines published to the parser
      # are derived from these only once the telegram has been accepted -- so
      # what gets published is always exactly what the checksum verified.
      raw_telegram = bytearray()

      # First element in telegram starts with "!"
      oversized = False
      while (not self.__stopper.is_set()) and (not line.startswith('!')):
        raw_telegram += raw

        if len(raw_telegram) > _MAX_TELEGRAM_BYTES:
          oversized = True
          break

        line, raw = self.__next_line()

        # Only in simulator mode; detect EOF in file
        if (not cfg.PRODUCTION) and line.startswith('EOF'):
          self.__stopper.set()
          logger.debug(f"EOF Detected in {cfg.SIMULATORFILE}")
          break

      if self.__stopper.is_set():
        break

      self.__telegram_count += 1
      now = time.time()

      if oversized:
        # No terminator found within a sane telegram size -- most likely 2+
        # telegrams merged after a corrupted "!" (see _MAX_TELEGRAM_BYTES).
        # Before giving up: a later telegram's header is often still findable
        # inside what was read, even though the leading one is unrecoverable.
        recovered = self.__try_recover_second_telegram(raw_telegram)
        if recovered is None:
          self.__corrupt_count += 1
          logger.warning(f"Discarding telegram #{self.__counter}: exceeded "
                          f"{_MAX_TELEGRAM_BYTES} bytes without a checksum line "
                          f"(2+ telegrams merged after a corrupted terminator?); "
                          f"{self.__corrupt_count} discarded, {self.__repaired_count} "
                          f"repaired, out of {self.__telegram_count} telegrams so far")
          self.__save_failed_telegram(raw_telegram)
          continue
        raw_telegram, line = recovered
      else:
        # `line`/`raw` now hold the checksum line ("!XXXX"); the DSMR CRC covers
        # bytes up to and including the "!" only, not the checksum digits after it.
        bang_idx = raw.find(b'!')
        if bang_idx >= 0:
          raw_telegram += raw[:bang_idx + 1]

      if not verify_crc(bytes(raw_telegram), line):
        # try_repair() may modify raw_telegram even when it fails; keep the
        # bytes as received for the failed-telegram dump.
        received = bytes(raw_telegram)
        repaired = self.__repairer.try_repair(raw_telegram, line, now)
        if repaired:
          self.__repaired_count += 1
        else:
          self.__corrupt_count += 1
          logger.warning(f"Discarding telegram #{self.__counter}: DSMR checksum invalid "
                          f"(noise on P1 line?); {self.__corrupt_count} discarded, "
                          f"{self.__repaired_count} repaired, out of {self.__telegram_count} "
                          f"telegrams so far")
          self.__save_failed_telegram(received, checksum_line=line)
          continue

      self.__repairer.update_baseline(raw_telegram, now)

      # counter as first field, then the lines of the accepted (possibly repaired) raw bytes
      self.__telegram[:] = [f"{self.__counter}"] + decode_lines(raw_telegram)

      try:
        self.__preprocess()
      except Exception:
        logger.exception("Error in __preprocess")


      file_path = "/mnt/ramdisk/p1_live.txt"

      with open(file_path, 'w') as file:
        # Write each line to the file
        for line in  self.__telegram:
          file.write(line + '\n')

      # Trigger that new telegram is available for MQTT
      self.__trigger.set()

      # In simulation mode, insert a delay
      if not cfg.PRODUCTION:
        # 1sec delay mimics dsmr behaviour, which transmits every 1sec a telegram
        time.sleep(1.0)

    logger.debug("<<")

  def run(self):
    logger.debug(">>")
    try:
      # In production, ReadSerial has infinite loop
      # In simulation, ReadSerial will return @ EOF
      self.__read_serial()

    except Exception as e:
      logger.error(f"Exception: {e}")

    finally:
      self.__tty.close()
      self.__stopper.set()

    logger.debug("<<")
