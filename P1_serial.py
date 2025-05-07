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

from datetime import datetime

import config as cfg

# Logging
import __main__
import logging
import os
script = os.path.basename(__main__.__file__)
script = os.path.splitext(script)[0]
logger = logging.getLogger(script + "." + __name__)


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

    self.__start_time = None
    self.__start_exported = None
    self.__last_average = None

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

    current_exported = None
    state_time = None
    current_export_power = None

    for element in self.__telegram:
      try:
        value = re.match(r"0-0:1\.0\.0\((\d{12})\*?S\)", element).group(1)
        state_time = datetime.strptime(value, "%y%m%d%H%M%S")
      except AttributeError:
        pass

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

    if self.__start_time is not None and state_time.minute % 15 == 0 and state_time.minute != self.__start_time.minute:
        self.__start_time = None

    if self.__start_time is None:
        self.__start_time = state_time
        self.__start_exported = current_exported
        cycle_exported = 0
        cycle_duration = 0
    else:
        cycle_exported = current_exported - self.__start_exported
        cycle_duration = state_time.timestamp() - self.__start_time.timestamp()

    if cycle_duration > 45:
        average = cycle_exported * 3600 / cycle_duration
    else:
        if current_export_power is not None:
            average = current_export_power
        else:
            average = self.__last_average

    # To make first minute of average value graph look smoother when instant power is changing fast
    if cycle_duration < 60 and self.__last_average is not None:
        average = (average + self.__last_average * 3) / 4


    # Insert the virtual entries in the dsmr telegram
    if average is not None:
        line = f"1-0:2.7.9({average:06.3f}*kW)"
        self.__telegram.append(line)
        self.__last_average = average

  # Sometimes we can have corrupted data. This method is fixing the line
  def __fix_line(self, line):
    if '<' in line:
      logging.info(f"Old line: {line}")
      line = line.replace('<', '(')
      logging.info(f"New line: {line}")
    return line

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

      # add a counter as first field to the list
      self.__counter += 1
      self.__telegram.append(f"{self.__counter}")

      # Decode from binary to ascii
      # Remove CR LF
      line = self.__tty.readline().decode('utf-8').rstrip()

      # First element in telegram starts with "!"
      nrof_elements = 0
      while (not self.__stopper.is_set()) and (not line.startswith('!')):
        try:
          line = self.__fix_line(line)
        except Exception:
          logger.exception("Error in __fix_line")

        self.__telegram.append(line)
        line = self.__tty.readline().decode('utf-8').rstrip()
        nrof_elements += 1
#        logger.debug(f"TELEGRAM Element = {line}")

        # Only in simulator mode; detect EOF in file
        if (not cfg.PRODUCTION) and line.startswith('EOF'):
          self.__stopper.set()
          logger.debug(f"EOF Detected in {cfg.SIMULATORFILE}")
          break

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
