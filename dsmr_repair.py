"""
DSMR telegram checksum validation and repair.

Every telegram from this meter ends with a line "!XXXX", a CRC-16 (poly
0xA001, "CRC-16/ARC") computed over every byte from the leading "/" through
the "!". dsmr_crc16()/verify_crc() implement that check.

A telegram that fails it is known -with certainty- to be damaged, but the
checksum alone doesn't say where. In practice (see the P1_serial CPU-usage/
noise investigation this was built for) almost every corruption is one
character, or a short run of them, flipped somewhere in an otherwise-perfect
~900 byte telegram -- and it very often lands in a spot where the correct
value, and hence the correct fix, can be deduced with near certainty:

  - this meter always sends the same lines in the same order and format, so
    when a telegram has as many lines as the last valid one, every line's
    OBIS code, fixed characters (".", "*", unit, ...) and never-changing
    values can be taken from it in one pass
    (TelegramRepairer._normalize_by_template); the per-field rules below
    that do the same are the fallback for when a line is lost or split
  - the OBIS code of a line is one of a small, fixed set (eg "1-0:1.8.0"); a
    one-character difference from a known code is very likely THE code,
    corrupted (TelegramRepairer._try_repair_obis_prefix)
  - this meter always sends the same codes in the same order, so a code too
    badly mangled for the above can still be identified by its position
    (TelegramRepairer._try_repair_obis_by_position)
  - a value field's format never changes -- decimal point, "*" separator and
    unit are always in the same place -- so a different character there (eg
    "00?000" for "00.000", "kva{" for "kvar") can only be corruption
    (TelegramRepairer._normalize_value_format)
  - cumulative (kWh/kvarh) counters only ever increase, and by a tiny amount
    per second, so an implausible jump can only be a corrupted digit
  - instantaneous power/voltage readings are bounded by what any real
    residential connection could ever produce
    (both: TelegramRepairer._try_repair_value_fields)
  - the equipment identifier never changes at all
    (TelegramRepairer._normalize_equipment_id)
  - neither does the telegram's header line (eg "/ESO5\\253880853_A")
    (TelegramRepairer._normalize_header)
  - nor, in practice, the text message field (0-0:96.13.0)
    (TelegramRepairer._normalize_text_message)
  - two fields glued into one line after a corrupted "\\r\\n" can often be
    told apart and split back up by the shape of the second one's value
    (TelegramRepairer._try_repair_glued_fields)

For all of these, a candidate fix is only ever accepted if it makes the
telegram's DSMR CRC-16 match again (the _normalize_* steps -- framing
characters, header, template, and the per-field fallbacks -- are the
exceptions, explained where they're implemented). That's what makes this safe rather
than a guess: a wrong single-character fix coincidentally also producing the
correct checksum is a 1-in-65536 shot, so a fix confirmed this way is, for
practical purposes, certain. If nothing here can fix a telegram, it's simply
left alone -- and, by the caller, dropped -- rather than published with an
unverified guess.

Every repair works on the raw bytes only. The decoded lines that get
published are derived from the (possibly repaired) raw bytes afterwards, via
decode_lines(), so what is published is always exactly what the checksum
verified.

Usage: one TelegramRepairer per serial connection. Call update_baseline() on
every telegram you accept (whether it needed repair or not) so later repairs
have a known-good value/unit/field-order to work from; call try_repair() on
one that failed verify_crc().

https://www.netbeheernederland.nl/ (DSMR P1 companion standard)
"""
import collections
import logging
import re

# Same convention every other module in this codebase uses (see eg P1_serial.py):
# nesting under "<script>." makes this a child of the "<script>" logger that
# log.py configures with the real handlers, so messages actually get written
# out instead of silently reaching no handler at all.
import __main__
import os
_script = os.path.splitext(os.path.basename(__main__.__file__))[0]
logger = logging.getLogger(_script + "." + __name__)


# ------------------------------------------------------------------------------------
# CRC-16/ARC (poly 0xA001, init 0x0000)
# ------------------------------------------------------------------------------------
def _build_crc16_table():
  table = []
  for byte in range(256):
    crc = byte
    for _ in range(8):
      crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    table.append(crc)
  return table


_CRC16_TABLE = _build_crc16_table()


def dsmr_crc16(data):
  """
  Args:
    :param bytes data: telegram bytes, from the leading "/" up to and including the "!"

  Returns:
    int: CRC-16/ARC checksum
  """
  crc = 0
  for byte in data:
    crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ byte) & 0xFF]
  return crc


def verify_crc(crc_input, checksum_line):
  """
    Validates a telegram's DSMR CRC-16 checksum.

  Args:
    :param bytes crc_input: raw telegram bytes, from "/" up to and including "!"
    :param str checksum_line: decoded checksum line, eg "!7ADC"

  Returns:
    bool: True if the checksum is present and matches
  """
  match = re.match(r"!([0-9A-Fa-f]{4})", checksum_line)
  if not match:
    logger.debug(f"No DSMR checksum found in '{checksum_line}'")
    return False

  expected = int(match.group(1), 16)
  computed = dsmr_crc16(crc_input)
  if expected != computed:
    logger.debug(f"DSMR checksum mismatch: expected {expected:04X}, computed {computed:04X}")
    return False
  return True


def iter_raw_lines(raw_telegram):
  """
    Splits raw telegram bytes into (offset, line) pairs, offset being the byte
    position of `line` within raw_telegram and line excluding its CR LF/LF
    terminator. Used to locate exactly where a repaired byte must be written.
  """
  data = bytes(raw_telegram)
  start = 0
  while start < len(data):
    nl = data.find(b'\n', start)
    end = len(data) if nl < 0 else nl
    line = data[start:end]
    if line.endswith(b'\r'):
      line = line[:-1]
    yield start, line
    if nl < 0:
      break
    start = nl + 1


def _field_lines(raw_telegram):
  """
    (offset, line) of every field line: everything after the header line and
    the blank line after it, without the final "!" line.
  """
  lines = list(iter_raw_lines(raw_telegram))[2:]
  if lines and lines[-1][1].startswith(b'!'):
    lines.pop()
  return lines


def decode_lines(raw_telegram):
  """
    Decodes an accepted telegram's raw bytes (from "/" through "!") into the
    lines the parser consumes: header, blank line, fields -- without the
    trailing "!" (the checksum line was never part of the published telegram).
  """
  lines = [line.decode('utf-8', errors='replace').rstrip()
           for _offset, line in iter_raw_lines(raw_telegram)]
  if lines and lines[-1].startswith('!'):
    lines.pop()
  return lines


# ------------------------------------------------------------------------------------
# Known telegram shape: OBIS codes, and the value fields' units/digit layout
# ------------------------------------------------------------------------------------

# OBIS codes this meter actually sends (from a live capture); used by
# _try_repair_obis_prefix to detect and repair a single corrupted character in
# a line's field identifier.
_KNOWN_OBIS_CODES = (
  "0-0:1.0.0", "1-0:1.8.0", "1-0:1.8.1", "1-0:1.8.2", "1-0:1.8.3", "1-0:1.8.4",
  "1-0:2.8.0", "1-0:3.8.0", "1-0:4.8.0", "1-0:1.7.0", "1-0:2.7.0", "1-0:3.7.0",
  "1-0:4.7.0", "1-0:21.7.0", "1-0:41.7.0", "1-0:61.7.0", "1-0:22.7.0", "1-0:42.7.0",
  "1-0:62.7.0", "1-0:23.7.0", "1-0:43.7.0", "1-0:63.7.0", "1-0:24.7.0", "1-0:44.7.0",
  "1-0:64.7.0", "1-0:32.7.0", "1-0:31.7.0", "1-0:52.7.0", "1-0:51.7.0", "1-0:72.7.0",
  "1-0:71.7.0", "1-0:14.7.0", "1-0:13.7.0", "1-0:33.7.0", "1-0:53.7.0", "1-0:73.7.0",
  "0-0:96.7.21", "0-0:96.13.0", "1-0:32.32.0", "1-0:52.32.0", "1-0:72.32.0",
  "1-0:32.36.0", "1-0:52.36.0", "1-0:72.36.0", "0-0:96.1.1",
)


def _value_regex(int_digits, frac_digits, digit):
  # value with `int_digits` before and `frac_digits` after the decimal point;
  # no decimal point at all when frac_digits is 0 (eg a current, "005")
  value = digit + b"{" + str(int_digits).encode() + b"}"
  if frac_digits:
    value += rb"\." + digit + b"{" + str(frac_digits).encode() + b"}"
  return value


def _compile_field_pattern(obis, unit, int_digits, frac_digits):
  # The digit positions of the value (group 1) are matched loosely -- any
  # character, not just 0-9 -- so the pattern still matches (and so
  # _fix_value_field can consider repairing) when noise corrupts a digit into
  # something that isn't one; see eg "0121(4.355" instead of "01214.355".
  # The decimal point itself is still required in the right place, so this
  # stays anchored to genuinely this field's value, not some other span of
  # bytes (by the time the CRC-gated repairs run, _normalize_value_format has
  # already put it back if it was corrupted). The separator (group 2) and the
  # unit (group 3) are matched by length only, not literally.
  return re.compile(re.escape(obis).encode() + rb"\((" + _value_regex(int_digits, frac_digits, rb"(?:.)")
                     + rb")(.)(.{" + str(len(unit)).encode() + rb"})\)")


def _compile_shape_pattern(unit, int_digits, frac_digits):
  # Same as _compile_field_pattern but without the OBIS code, and with real
  # digits and the literal unit -- used to recognize a value*unit by shape
  # alone, when the OBIS code in front of it is missing or unrecoverable (see
  # _try_repair_glued_fields).
  return re.compile(b"(" + _value_regex(int_digits, frac_digits, rb"\d") + rb")(.)"
                     + re.escape(unit).encode() + rb"\)")


# Every value field this module knows how to repair: (obis, unit, digits
# before/after the decimal point -- 0 after meaning no decimal point). All of
# them have a fixed format, eg "00.000*kW" or "000*A". One source of truth for
# the format normalization, the per-category spec lists (used to pick a
# plausibility rule) and the shape-keyed lookup used by
# _try_repair_glued_fields (used to guess a field's identity from its value
# alone, when its own OBIS code didn't survive).
_VALUE_FIELDS = (
  ("1-0:1.8.0", "kWh", 6, 3), ("1-0:1.8.1", "kWh", 6, 3), ("1-0:1.8.2", "kWh", 6, 3),
  ("1-0:1.8.3", "kWh", 6, 3), ("1-0:1.8.4", "kWh", 6, 3), ("1-0:2.8.0", "kWh", 6, 3),
  ("1-0:3.8.0", "kvarh", 6, 3), ("1-0:4.8.0", "kvarh", 6, 3),
  ("1-0:1.7.0", "kW", 2, 3), ("1-0:2.7.0", "kW", 2, 3),
  ("1-0:21.7.0", "kW", 2, 3), ("1-0:41.7.0", "kW", 2, 3), ("1-0:61.7.0", "kW", 2, 3),
  ("1-0:22.7.0", "kW", 2, 3), ("1-0:42.7.0", "kW", 2, 3), ("1-0:62.7.0", "kW", 2, 3),
  ("1-0:32.7.0", "V", 3, 1), ("1-0:52.7.0", "V", 3, 1), ("1-0:72.7.0", "V", 3, 1),
  ("1-0:3.7.0", "kvar", 2, 3), ("1-0:4.7.0", "kvar", 2, 3),
  ("1-0:23.7.0", "kvar", 2, 3), ("1-0:43.7.0", "kvar", 2, 3), ("1-0:63.7.0", "kvar", 2, 3),
  ("1-0:24.7.0", "kvar", 2, 3), ("1-0:44.7.0", "kvar", 2, 3), ("1-0:64.7.0", "kvar", 2, 3),
  ("1-0:31.7.0", "A", 3, 0), ("1-0:51.7.0", "A", 3, 0), ("1-0:71.7.0", "A", 3, 0),
  ("1-0:14.7.0", "Hz", 2, 1),
)


def _value_format(unit, int_digits, frac_digits):
  fmt = b"0" * int_digits
  if frac_digits:
    fmt += b"." + b"0" * frac_digits
  return fmt + b"*" + unit.encode() + b")"


# The fixed format of every field's value (everything after the "("): "0"
# marks a digit, "S" the timestamp's "S"/"W" (summer/winter time), and every
# other character is always exactly that character. See _apply_format.
_VALUE_FORMATS = {obis.encode(): _value_format(unit, i, f) for obis, unit, i, f in _VALUE_FIELDS}
_VALUE_FORMATS[b"0-0:1.0.0"] = b"000000000000S)"

# Power factors: same idea, but the value may be preceded by a "-".
_SIGNED_VALUE_FORMATS = {code.encode(): b"0.000)"
                         for code in ("1-0:13.7.0", "1-0:33.7.0", "1-0:53.7.0", "1-0:73.7.0")}


def _apply_format(code, value, template_value=None):
  """
    Puts back every fixed character of `code`'s value -- decimal point, "*",
    unit, ")", a power factor's "-" -- keeping the received characters at the
    digit positions (a wrong digit is for _try_repair_value_fields), eg
    "0?590)" -> "0.590)", "00?000*kW)" -> "00.000*kW)", "000%A)" -> "000*A)".
    The timestamp's "S"/"W" is kept if it's either; otherwise it's taken from
    template_value (the last valid telegram's), or "S" without one.

  Returns:
    bytes: the value with its fixed characters put back, or None if `code`
    has no known format or the value doesn't have the format's length
  """
  fmt, sign = _VALUE_FORMATS.get(code), b''
  if fmt is None:
    fmt = _SIGNED_VALUE_FORMATS.get(code)
    if fmt is None:
      return None
    if len(value) == len(fmt) + 1:
      sign, value = b'-', value[1:]
      template_value = None
  if len(value) != len(fmt):
    return None

  season = b'S'
  if template_value and len(template_value) == len(fmt):
    season = bytes([template_value[fmt.find(b'S')]]) if b'S' in fmt else b'S'
  fixed = bytearray()
  for v, f in zip(value, fmt):
    if f == ord('0'):
      fixed.append(v)
    elif f == ord('S'):
      fixed += bytes([v]) if v in b'SW' else season
    else:
      fixed.append(f)
  return sign + bytes(fixed)

# Cumulative registers: monotonic, near-zero expected delta per second.
_COUNTER_FIELD_SPECS = [(obis, _compile_field_pattern(obis, unit, i, f))
                         for obis, unit, i, f in _VALUE_FIELDS if unit in ("kWh", "kvarh")]
_MAX_COUNTER_POWER_KW = 50      # generous ceiling used to bound a counter's plausible per-second delta
_MIN_COUNTER_DELTA_KWH = 0.01   # floor on that bound, for back-to-back telegrams

# Instantaneous power: not monotonic, but bounded by any realistic residential connection.
_POWER_FIELD_SPECS = [(obis, _compile_field_pattern(obis, unit, i, f))
                       for obis, unit, i, f in _VALUE_FIELDS if unit == "kW"]
_MAX_INSTANT_POWER_KW = 40

# Phase voltage: bounded by what the LV grid can plausibly deliver, generous enough
# to never flag a real sag/swell event (the meter separately counts those).
_VOLTAGE_FIELD_SPECS = [(obis, _compile_field_pattern(obis, unit, i, f))
                         for obis, unit, i, f in _VALUE_FIELDS if unit == "V"]
_MIN_VOLTAGE_V = 150.0
_MAX_VOLTAGE_V = 280.0

# Instantaneous reactive power: same shape and bounding logic as real power above.
_REACTIVE_POWER_FIELD_SPECS = [(obis, _compile_field_pattern(obis, unit, i, f))
                                for obis, unit, i, f in _VALUE_FIELDS if unit == "kvar"]
_MAX_INSTANT_KVAR = 40

# Phase current: bounded by any realistic residential main fuse.
_CURRENT_FIELD_SPECS = [(obis, _compile_field_pattern(obis, unit, i, f))
                         for obis, unit, i, f in _VALUE_FIELDS if unit == "A"]
_MAX_CURRENT_A = 100

# Grid frequency: nominally 50 Hz; the grid never strays anywhere near these bounds.
_FREQUENCY_FIELD_SPECS = [(obis, _compile_field_pattern(obis, unit, i, f))
                           for obis, unit, i, f in _VALUE_FIELDS if unit == "Hz"]
_MIN_FREQUENCY_HZ = 45.0
_MAX_FREQUENCY_HZ = 55.0

_ALL_VALUE_FIELD_SPECS = (_COUNTER_FIELD_SPECS + _POWER_FIELD_SPECS + _VOLTAGE_FIELD_SPECS
                           + _REACTIVE_POWER_FIELD_SPECS + _CURRENT_FIELD_SPECS
                           + _FREQUENCY_FIELD_SPECS)

# Equipment identifier: fixed for the lifetime of the meter. In a telegram that
# failed its checksum it's replaced by the last known-good value outright, not
# by guessing digits -- see TelegramRepairer._normalize_equipment_id. Matched
# loosely -- any characters up to ")" -- so a corrupted, non-digit value is
# still found.
_EQUIPMENT_ID_OBIS = "0-0:96.1.1"
_EQUIPMENT_ID_PATTERN = re.compile(re.escape(_EQUIPMENT_ID_OBIS).encode() + rb"\(([^)\r\n]*)\)")

# Text message: a hex-encoded status string (eg "setP1_extended_3ph") that
# rarely if ever changes; in a telegram that failed its checksum it's replaced
# by the last known-good value (see _normalize_text_message). The value can
# legitimately be empty (seen as "0-0:96.13.0()" in real captures). Matched
# loosely -- any characters up to ")" -- so a corrupted, non-hex value (eg
# "...657874?56E...") is still found.
_TEXT_MESSAGE_OBIS = "0-0:96.13.0"
_TEXT_MESSAGE_PATTERN = re.compile(re.escape(_TEXT_MESSAGE_OBIS).encode() + rb"\(([^)\r\n]*)\)")

# Fields whose whole value practically never changes: equipment id, text
# message, number of power failures, and the sag/swell counters. In a telegram
# that failed its checksum, _normalize_by_template copies them whole from the
# last valid telegram. (A real change arrives in a telegram with a valid
# checksum and becomes the new baseline; one that happens to coincide with
# corruption elsewhere just costs that one telegram.)
# How many accepted telegrams a digit must have stayed the same in, to count as
# stable for _try_repair_stable_digit.
_STABLE_HISTORY = 10

_CONSTANT_FIELD_CODES = frozenset(code.encode() for code in (
  _EQUIPMENT_ID_OBIS, _TEXT_MESSAGE_OBIS, "0-0:96.7.21",
  "1-0:32.32.0", "1-0:52.32.0", "1-0:72.32.0", "1-0:32.36.0", "1-0:52.36.0", "1-0:72.36.0",
))

# shape (unit, int_digits, frac_digits) -> compiled shape-only pattern / candidate OBIS codes
_SHAPE_PATTERNS = {}
_CANDIDATES_BY_SHAPE = {}
for _obis, _unit, _i, _f in _VALUE_FIELDS:
  _shape = (_unit, _i, _f)
  _SHAPE_PATTERNS.setdefault(_shape, _compile_shape_pattern(_unit, _i, _f))
  _CANDIDATES_BY_SHAPE.setdefault(_shape, []).append(_obis)
del _obis, _unit, _i, _f, _shape


class TelegramRepairer:
  """
    Learns this meter's normal telegram shape -- field order, units, counter
    values, header, equipment id, text message -- from every telegram it's
    told was accepted, and uses that to repair a telegram that failed its
    DSMR checksum. See the module docstring for the overall approach and
    safety argument.
  """

  def __init__(self):
    self.last_header = None
    self.last_equipment_id = None
    self.last_text_message = None
    self.last_counter_values = {}
    self.field_sequence = []
    self.last_field_lines = []
    self.recent_field_lines = collections.deque(maxlen=_STABLE_HISTORY)
    self.last_reading_time = None

  def update_baseline(self, raw_telegram, now):
    """
      Records this (now known-good) telegram's header, counter/equipment-id/
      unit values, field order and time -- call this on every telegram you
      accept, whether it needed repair or not, so later repairs have
      somewhere to draw from.
    """
    data = bytes(raw_telegram)

    header_offset, header_line = next(iter_raw_lines(raw_telegram), (0, b''))
    if header_line:
      self.last_header = header_line

    for obis, pattern in _COUNTER_FIELD_SPECS:
      match = pattern.search(data)
      if match:
        try:
          self.last_counter_values[obis] = float(match.group(1))
        except ValueError:
          pass

    match = _EQUIPMENT_ID_PATTERN.search(data)
    if match:
      self.last_equipment_id = match.group(1)

    match = _TEXT_MESSAGE_PATTERN.search(data)
    if match:
      self.last_text_message = match.group(1)

    # This meter always sends the same OBIS codes in the same order, in the
    # same format; record every field line (skipping the header line and the
    # blank line after it, so index 0 here is the telegram's first actual
    # field) as the template for _normalize_by_template, and the codes alone
    # for _try_repair_obis_by_position.
    field_lines = [bytes(line) for _offset, line in _field_lines(raw_telegram) if line.find(b'(') > 0]
    if field_lines:
      self.last_field_lines = field_lines
      self.recent_field_lines.append(field_lines)
      self.field_sequence = [line[:line.find(b'(')] for line in field_lines]

    self.last_reading_time = now

  def try_repair(self, raw_telegram, checksum_line, now):
    """
      Attempts to fix a telegram that has already failed verify_crc(),
      trying each repair mechanism below in turn and stopping as soon as one
      succeeds. Only raw_telegram is changed; the caller derives the decoded
      lines from it afterwards (decode_lines()).

    Args:
      :param bytearray raw_telegram: the telegram's raw bytes; patched in
        place (also when no fix is found -- see the normalizations below,
        so pass a copy if the bytes as received are still needed)
      :param str checksum_line: the "!XXXX" the telegram failed to match
      :param float now: time.time(), used for the counter-delta plausibility check

    Returns:
      bool: True if raw_telegram now matches checksum_line
    """
    # Normalizations first, unconditionally (see the _normalize_* methods);
    # this alone may already restore the checksum, and if not, leaves any
    # other, genuine single-point corruption for the CRC-gated repairs below
    # to find. When the telegram has as many field lines as the last valid
    # one, every line is fixed in one pass against it as a template;
    # otherwise (a line lost or split) the per-field rules are used, OBIS
    # codes first since the other patterns are anchored on them.
    normalized = [self._normalize_frame_chars(raw_telegram),
                  self._normalize_header(raw_telegram),
                  self._split_glued_by_template(raw_telegram)]
    if self.last_field_lines and len(_field_lines(raw_telegram)) == len(self.last_field_lines):
      normalized.append(self._normalize_by_template(raw_telegram))
    else:
      normalized += [self._normalize_obis_codes(raw_telegram),
                     self._normalize_equipment_id(raw_telegram),
                     self._normalize_text_message(raw_telegram),
                     self._normalize_value_format(raw_telegram)]
    repaired = ((any(normalized) and verify_crc(bytes(raw_telegram), checksum_line))
            or self._try_repair_non_digit(raw_telegram, checksum_line)
            or self._try_repair_stable_digit(raw_telegram, checksum_line)
            or self._try_repair_obis_prefix(raw_telegram, checksum_line)
            or self._try_repair_obis_by_position(raw_telegram, checksum_line)
            or self._try_repair_value_fields(raw_telegram, checksum_line, now)
            or self._try_repair_glued_fields(raw_telegram, checksum_line))
    if repaired and not self._is_well_formed(raw_telegram):
      # The CRC-gated repairs try many candidates, so across thousands of
      # noisy telegrams one occasionally matches the checksum by pure chance
      # (1 in 65536 each) -- eg a garbage-filled telegram "fixed" by an
      # unrelated one-character OBIS change. A real repair always yields a
      # well-formed telegram; a chance match practically never does.
      logger.warning("Rejected repair: checksum matches, but the telegram isn't well-formed "
                      "(most likely a chance checksum match)")
      return False
    return repaired

  def _is_well_formed(self, raw_telegram):
    """
      True if every field line has the expected OBIS code -- in the order of
      the last valid telegram, if known, otherwise any known code -- and every
      value with a known format fits it exactly (digits where the format has
      digits, every fixed character as in the format); any other value must
      be hex digits followed by ")".
    """
    lines = [bytes(line) for _offset, line in _field_lines(raw_telegram)]
    if self.field_sequence:
      if len(lines) != len(self.field_sequence):
        return False
      codes = self.field_sequence
    else:
      codes = [line[:line.find(b'(')] for line in lines]
      if not all(code in self._known_obis_codes() for code in codes):
        return False

    for line, code in zip(lines, codes):
      if not line.startswith(code + b'('):
        return False
      value = line[len(code) + 1:]
      fmt = _VALUE_FORMATS.get(code)
      if fmt is None and code in _SIGNED_VALUE_FORMATS:
        fmt = _SIGNED_VALUE_FORMATS[code]
        if len(value) == len(fmt) + 1 and value.startswith(b'-'):
          value = value[1:]
      if fmt is None:
        if not re.fullmatch(rb"[0-9A-Fa-f]*\)", value):
          return False
      elif len(value) != len(fmt) or not all(
          (ord('0') <= v <= ord('9')) if f == ord('0') else (v in b'SW') if f == ord('S') else v == f
          for v, f in zip(value, fmt)):
        return False
    return True

  @staticmethod
  def _normalize_frame_chars(raw_telegram):
    """
      Replaces "<" with "(" and ">" with "*" -- the long-known framing
      corruption on this line. Neither character ever occurs in a valid DSMR
      telegram, so, like _normalize_value_format, this runs unconditionally rather
      than CRC-gated: the telegram is still only accepted if it (or a further
      repair on top) passes the real checksum.

    Returns:
      bool: True if anything was replaced (raw_telegram is updated in place)
    """
    if b'<' not in raw_telegram and b'>' not in raw_telegram:
      return False
    for _offset, line in iter_raw_lines(raw_telegram):
      if b'<' in line or b'>' in line:
        logger.info(f"Normalized framing characters: {bytes(line)!r}")
    raw_telegram[:] = bytes(raw_telegram).replace(b'<', b'(').replace(b'>', b'*')
    return True

  def _split_glued_by_template(self, raw_telegram):
    """
      When the telegram has fewer field lines than the last valid one, some
      "\r\n" line breaks were lost, gluing two lines into one -- often along
      with the end of the first field and the start of the second, eg
      "1-0:3.7.0(00.512*-W..%C!nK\\...7.0(00.098*kvar)" for
      "1-0:3.7.0(00.512*kvar)" + "1-0:4.7.0(00.098*kvar)". Walking the lines
      alongside the template (last_field_lines), a line clearly longer than
      its template line (by more than half the next one) is taken to be it
      glued to the next: it's split into its first len(template line) bytes
      and its last len(next template line) bytes, with "\r\n" between them;
      whatever was in between is noise and dropped (the two parts may also
      overlap, if bytes were lost). The codes and fixed characters of both
      halves are then fixed by _normalize_by_template, which can run once the
      line count matches again.

      Not CRC-gated itself: the telegram is still only accepted if it passes
      the real checksum afterwards.

    Returns:
      bool: True if any line was split (raw_telegram is updated in place)
    """
    template = self.last_field_lines
    lines = _field_lines(raw_telegram)
    missing = len(template) - len(lines)
    if not template or missing <= 0:
      return False

    edits = []  # (offset, length replaced, replacement)
    t = 0
    for offset, line in lines:
      if t + 1 < len(template) and missing > 0 and len(line) > len(template[t]) + len(template[t + 1]) // 2:
        first, second = template[t], template[t + 1]
        edits.append((offset, len(line), bytes(line[:len(first)]) + b"\r\n" + bytes(line[-len(second):])))
        missing -= 1
        t += 2
      else:
        t += 1

    for offset, length, replacement in reversed(edits):
      logger.warning(f"Split glued line: {bytes(raw_telegram[offset:offset + length])!r} -> {replacement!r}")
      raw_telegram[offset:offset + length] = replacement
    return bool(edits)

  def _normalize_by_template(self, raw_telegram):
    """
      Fixes every field line in one pass, using the same line of the last
      valid telegram as a template -- this meter always sends the same codes
      in the same order and format. Only called when the telegram has exactly
      as many field lines as that one. For each line, with P its template:

        - OBIS code and "(": P's code is expected, and so is its length. The
          line's own code is located as follows and replaced by P's:
            - "(" right after len(code) characters: those characters are the
              code, whatever they are (eg "LKL\\x1d\\x1b3.7.0(" -> "1-0:63.7.0(",
              or "1-0:4.(.0(" where _normalize_frame_chars turned a "<" in the
              code into "(");
            - the code, one other byte, then "(": that byte was inserted (eg a
              NUL) and is removed;
            - the code, then something other than "(": the "(" was corrupted;
            - exactly as long as P: the code and "(" are corrupted but in
              place, so the first len(code) + 1 characters (eg
              "1-0:1.8.&'LL\\x18\\x1800.000*kWh)" -> "1-0:1.8.3(LL...");
            - otherwise (a character lost or inserted in the code): everything
              up to the first "(".
          Any other line without a "(" is left alone.
        - value, for the fields in _CONSTANT_FIELD_CODES (equipment id, text
          message, power failures, sag/swell counters): copied whole from P.
        - value, for every field with a known format (_VALUE_FORMATS,
          _SIGNED_VALUE_FORMATS): its fixed characters are put back by
          _apply_format -- independent of P's value, so eg a power factor
          that changed sign since P is handled too.
        - value, for any other field, when it has the same length as P's:
          every position where P has a non-digit is copied from P.
        In both cases the digits are kept as received (a wrong digit is left
        to _try_repair_value_fields), and a value of the wrong length is kept.

      Not CRC-gated itself: the telegram is still only accepted if it passes
      the real checksum afterwards.

    Returns:
      bool: True if any line was changed (raw_telegram is updated in place)
    """
    edits = []  # (offset, length replaced, replacement)
    for (offset, line), template in zip(_field_lines(raw_telegram), self.last_field_lines):
      code = template[:template.find(b'(')]
      if line.startswith(code + b'(') or line[len(code):len(code) + 1] == b'(':
        value_start = len(code) + 1
      elif line.startswith(code) and line[len(code) + 1:len(code) + 2] == b'(':
        value_start = len(code) + 2  # an extra byte (eg NUL) inserted
      elif line.startswith(code) and len(line) > len(code):
        value_start = len(code) + 1  # the "(" itself corrupted
      elif len(line) == len(template):
        value_start = len(code) + 1  # code and "(" corrupted, but in place
      elif line.find(b'(') >= 0:
        value_start = line.find(b'(') + 1
      else:
        continue

      value, template_value = bytes(line[value_start:]), template[len(code) + 1:]
      if code in _CONSTANT_FIELD_CODES:
        value = template_value
      else:
        formatted = _apply_format(code, value, template_value)
        if formatted is not None:
          value = formatted
        elif len(value) == len(template_value):
          # a field without a known format: copy the template's non-digits
          value = bytes(v if 0x30 <= t <= 0x39 else t for v, t in zip(value, template_value))

      fixed = code + b'(' + value
      if fixed != line:
        edits.append((offset, len(line), fixed))

    # apply back to front so earlier offsets stay valid when lengths differ
    for offset, length, fixed in reversed(edits):
      logger.warning(f"Normalized line: {bytes(raw_telegram[offset:offset + length])!r} -> {fixed!r}")
      raw_telegram[offset:offset + length] = fixed
    return bool(edits)

  def _normalize_obis_codes(self, raw_telegram):
    """
      Fallback for when _normalize_by_template can't be used (right after
      startup, or a line lost or split): a code before the first "(" that
      isn't a known code is replaced by the one known code a single character
      away from it (eg "1-0:62.7?0" -> "1-0:62.7.0"); a code one character
      from several known codes (eg "1-0:1.8.5") is left alone. Any number of
      lines can be fixed at once. Not CRC-gated itself: the telegram is still
      only accepted if it passes the real checksum afterwards.

    Returns:
      bool: True if any code was corrected (raw_telegram is updated in place)
    """
    known = self._known_obis_codes()
    edits = []  # (offset, length replaced, replacement)
    for offset, line in _field_lines(raw_telegram):
      prefix = line[:max(line.find(b'('), 0)]
      if not prefix or prefix in known:
        continue
      near = [code for code in known
              if len(code) == len(prefix) and sum(a != b for a, b in zip(code, prefix)) == 1]
      if len(near) == 1:
        edits.append((offset, len(prefix), near[0]))

    # apply back to front so earlier offsets stay valid when lengths differ
    for offset, length, replacement in reversed(edits):
      logger.warning(f"Normalized OBIS code: '{bytes(raw_telegram[offset:offset + length]).decode(errors='replace')}'"
                      f" -> '{replacement.decode()}'")
      raw_telegram[offset:offset + length] = replacement
    return bool(edits)

  def _normalize_header(self, raw_telegram):
    """
      Replaces the header (the first line, eg "/ESO5\\253880853_A") with the
      one from the last telegram that passed its checksum, whenever they
      differ -- whatever the corruption, including a changed length. The
      header is fixed for the life of the meter. Not CRC-gated itself: the
      telegram is still only accepted if it passes the real checksum
      afterwards.

    Returns:
      bool: True if the header was replaced (raw_telegram is updated in place)
    """
    if self.last_header is None:
      return False

    _offset, current = next(iter_raw_lines(raw_telegram), (0, b''))
    if current == self.last_header:
      return False

    raw_telegram[0:len(current)] = self.last_header
    logger.warning(f"Normalized header: '{current.decode(errors='replace')}' -> "
                    f"'{self.last_header.decode(errors='replace')}' (last known-good value)")
    return True

  def _normalize_equipment_id(self, raw_telegram):
    """
      Replaces the equipment identifier (0-0:96.1.1) value with the one from
      the last telegram that passed its checksum, whenever they differ --
      whatever the corruption, including a changed length. The equipment id
      is fixed for the life of the meter. Not CRC-gated itself: the telegram
      is still only accepted if it passes the real checksum afterwards.

    Returns:
      bool: True if the value was replaced (raw_telegram is updated in place)
    """
    if self.last_equipment_id is None:
      return False

    match = _EQUIPMENT_ID_PATTERN.search(bytes(raw_telegram))
    if not match or match.group(1) == self.last_equipment_id:
      return False

    start, end = match.span(1)
    raw_telegram[start:end] = self.last_equipment_id
    logger.warning(f"Normalized {_EQUIPMENT_ID_OBIS}: '{match.group(1).decode(errors='replace')}' -> "
                    f"'{self.last_equipment_id.decode()}' (last known-good value)")
    return True

  def _normalize_text_message(self, raw_telegram):
    """
      Replaces the text message (0-0:96.13.0) value with the one from the last
      telegram that passed its checksum, whenever they differ -- whatever the
      corruption, including a changed length. Like the other normalizations
      it isn't CRC-gated itself: the telegram is still only accepted if it
      passes the real checksum afterwards (a genuinely changed message arrives
      in a telegram with a valid checksum, which never gets here, and becomes
      the new baseline).

    Returns:
      bool: True if the value was replaced (raw_telegram is updated in place)
    """
    if self.last_text_message is None:
      return False

    match = _TEXT_MESSAGE_PATTERN.search(bytes(raw_telegram))
    if not match or match.group(1) == self.last_text_message:
      return False

    start, end = match.span(1)
    raw_telegram[start:end] = self.last_text_message
    logger.warning(f"Normalized {_TEXT_MESSAGE_OBIS}: '{match.group(1).decode(errors='replace')}' -> "
                    f"'{self.last_text_message.decode()}' (last known-good value)")
    return True

  def _known_obis_codes(self):
    return set(code.encode() for code in _KNOWN_OBIS_CODES) | set(self.field_sequence)

  def _normalize_value_format(self, raw_telegram):
    """
      Fallback for when _normalize_by_template can't be used: for every field
      line whose code (before the first "(") has a known format, puts back the
      value's fixed characters with _apply_format -- eg "1-0:53.7.0(0?590)"
      -> "1-0:53.7.0(0.590)", "00.324?kW" -> "00.324*kW", "kva{" -> "kvar".
      Applied unconditionally, without its own CRC check: these characters are
      fixed for the life of the meter, so a different one can only be
      corruption, and the telegram is still only accepted if it passes the
      real checksum afterwards.

    Returns:
      bool: True if anything was corrected (raw_telegram is updated in place)
    """
    fixed_any = False
    for offset, line in _field_lines(raw_telegram):
      paren = line.find(b'(')
      if paren <= 0:
        continue
      value = bytes(line[paren + 1:])
      formatted = _apply_format(bytes(line[:paren]), value)
      if formatted is None or formatted == value:
        continue
      # same length, so the other lines' offsets stay valid
      raw_telegram[offset + paren + 1:offset + len(line)] = formatted
      logger.warning(f"Normalized {line[:paren].decode(errors='replace')} format: "
                      f"{value!r} -> {formatted!r}")
      fixed_any = True
    return fixed_any

  def _try_repair_non_digit(self, raw_telegram, checksum_line):
    """
      Every field with a known format (_VALUE_FORMATS, _SIGNED_VALUE_FORMATS)
      says exactly where its digits are, so a non-digit there is certainly
      corrupt -- eg the timestamp "2609;3222639S".
        - exactly one such position in the whole telegram: tries "0"-"9"
          there and accepts the one that restores the checksum;
        - several: tries one candidate, with each of them filled in with the
          digit the last valid telegram had at that position (when the
          telegram has as many field lines as that one) -- right for values
          that rarely change, eg an unused tariff's "000000.000", or a
          counter's high digits.
      Not more: trying all 100+ combinations of several positions would make
      a wrong one passing the checksum by chance too likely.

    Returns:
      bool: True if a fix was applied (raw_telegram is updated in place)
    """
    lines = _field_lines(raw_telegram)
    templates = self.last_field_lines if len(self.last_field_lines) == len(lines) else [None] * len(lines)

    positions = []  # (byte offset in raw_telegram, the template's digit there or None)
    for (offset, line), template in zip(lines, templates):
      paren = line.find(b'(')
      if paren <= 0:
        continue
      code, value, value_offset = bytes(line[:paren]), line[paren + 1:], offset + paren + 1
      template_value = template[len(code) + 1:] if template and template.startswith(code + b'(') else None
      fmt = _VALUE_FORMATS.get(code)
      if fmt is None:
        fmt = _SIGNED_VALUE_FORMATS.get(code)
        if fmt is None:
          continue
        if len(value) == len(fmt) + 1:
          value, value_offset = value[1:], value_offset + 1
        if template_value is not None and len(template_value) == len(fmt) + 1:
          template_value = template_value[1:]
      if len(value) != len(fmt):
        continue
      if template_value is not None and len(template_value) != len(fmt):
        template_value = None
      for i, (v, f) in enumerate(zip(value, fmt)):
        if f == ord('0') and not ord('0') <= v <= ord('9'):
          positions.append((value_offset + i, template_value[i] if template_value is not None else None))

    if len(positions) == 1:
      candidates = [[(positions[0][0], digit)] for digit in b"0123456789"]
    elif positions and all(digit is not None for _pos, digit in positions):
      candidates = [positions]
    else:
      return False

    for candidate in candidates:
      candidate_raw = bytearray(raw_telegram)
      for pos, digit in candidate:
        candidate_raw[pos] = digit
      if verify_crc(bytes(candidate_raw), checksum_line):
        logger.warning("Repaired non-digit(s): " + ", ".join(
          f"{bytes([raw_telegram[pos]])!r} -> {chr(digit)!r} at byte {pos}" for pos, digit in candidate)
          + "; checksum now valid")
        raw_telegram[:] = candidate_raw
        return True
    return False

  def _try_repair_stable_digit(self, raw_telegram, checksum_line):
    """
      Some digits never change from one telegram to the next -- eg a phase's
      power that's always 00.000, an unused tariff, the high digits of a
      counter or of the voltage. A digit that was the same in each of the
      last _STABLE_HISTORY accepted telegrams is "stable"; noise that turns
      it into another digit (eg "1-0:62.7.0(00.008*kW)" for "00.000") leaves
      a well-formed, plausible-looking value that nothing else can spot. If
      1 to 3 stable digits differ, tries reverting each one alone, then all
      of them, and accepts the first candidate that restores the checksum --
      at most 4 candidates, so a chance checksum match stays negligible.
      Trying every digit instead would not be safe: for a single telegram,
      several different single-digit changes can match the checksum.

    Returns:
      bool: True if a fix was applied (raw_telegram is updated in place)
    """
    history = self.recent_field_lines
    lines = _field_lines(raw_telegram)
    if len(history) < _STABLE_HISTORY or any(len(past) != len(lines) for past in history):
      return False

    positions = []  # (byte offset in raw_telegram, the stable digit)
    for index, (offset, line) in enumerate(lines):
      past = [lines_[index] for lines_ in history]
      if any(len(p) != len(line) for p in past):
        continue
      for i, c in enumerate(line):
        stable = past[0][i]
        if (ord('0') <= c <= ord('9') and ord('0') <= stable <= ord('9') and c != stable
            and all(p[i] == stable for p in past)):
          positions.append((offset + i, stable))
    if not 1 <= len(positions) <= 3:
      return False

    candidates = [[position] for position in positions] + ([positions] if len(positions) > 1 else [])
    for candidate in candidates:
      candidate_raw = bytearray(raw_telegram)
      for pos, digit in candidate:
        candidate_raw[pos] = digit
      if verify_crc(bytes(candidate_raw), checksum_line):
        logger.warning("Repaired stable digit(s): " + ", ".join(
          f"{chr(raw_telegram[pos])!r} -> {chr(digit)!r} at byte {pos}" for pos, digit in candidate)
          + "; checksum now valid")
        raw_telegram[:] = candidate_raw
        return True
    return False

  def _try_repair_obis_prefix(self, raw_telegram, checksum_line):
    """
      Looks for a line whose OBIS code is exactly one character away from a
      known code (eg "1=0:1.8.0" vs "1-0:1.8.0") and, if fixing it restores
      the telegram's checksum, applies the fix.

    Returns:
      bool: True if a fix was applied (raw_telegram is updated in place)
    """
    for offset, line in iter_raw_lines(raw_telegram):
      paren = line.find(b'(')
      if paren <= 0:
        continue
      prefix = line[:paren]

      for known in _KNOWN_OBIS_CODES:
        known_b = known.encode()
        if len(known_b) != len(prefix) or prefix == known_b:
          continue
        diffs = [i for i in range(len(prefix)) if prefix[i] != known_b[i]]
        if len(diffs) != 1:
          continue

        pos = diffs[0]
        candidate_raw = bytearray(raw_telegram)
        candidate_raw[offset + pos] = known_b[pos]
        if not verify_crc(bytes(candidate_raw), checksum_line):
          continue

        old_char, new_char = chr(prefix[pos]), chr(known_b[pos])
        raw_telegram[offset + pos] = known_b[pos]
        logger.warning(f"Repaired OBIS code: '{prefix.decode(errors='replace')}' -> '{known}' "
                        f"(char {pos}: '{old_char}' -> '{new_char}'); checksum now valid")
        return True
    return False

  def _try_repair_obis_by_position(self, raw_telegram, checksum_line):
    """
      This meter always sends the same OBIS codes in the same order (see
      update_baseline). When a line's OBIS code is too corrupted for a
      Hamming-distance match (_try_repair_obis_prefix, which only catches a
      single wrong character) to find -- eg several characters wrong at
      once, such as "1KL..3.7.0" for "1-0:23.7.0" -- this instead identifies
      the field purely by *where* it sits among the telegram's fields, and
      substitutes whatever code the learned order says belongs at that
      position, verified via checksum like every other repair here.

    Returns:
      bool: True if a fix was applied (raw_telegram is updated in place)
    """
    if not self.field_sequence:
      return False

    # position 0 is the header line, position 1 the blank line after it (see
    # update_baseline); fields start at position 2.
    fields = list(iter_raw_lines(raw_telegram))[2:]

    for position, (offset, line) in enumerate(fields):
      if position >= len(self.field_sequence):
        break

      paren = line.find(b'(')
      if paren <= 0:
        continue
      prefix = line[:paren]
      expected = self.field_sequence[position]
      if prefix == expected:
        continue

      candidate_raw = bytearray(raw_telegram)
      candidate_raw[offset:offset + len(prefix)] = expected
      if not verify_crc(bytes(candidate_raw), checksum_line):
        continue

      raw_telegram[offset:offset + len(prefix)] = expected
      logger.warning(f"Repaired OBIS code by position (field #{position}): "
                      f"'{prefix.decode(errors='replace')}' -> '{expected.decode()}'; "
                      f"checksum now valid")
      return True
    return False

  def _fix_value_field(self, raw_telegram, checksum_line, obis, pattern, is_plausible):
    """
      If `obis`'s value in raw_telegram fails `is_plausible` (or isn't a
      number at all), tries every single-digit substitution of it and accepts
      the first plausible candidate that restores the telegram's checksum.
      (A wrong decimal point, separator or unit is handled earlier and
      unconditionally, by _normalize_value_format, before this is ever called.)

    Args:
      :param callable is_plausible: float -> bool

    Returns:
      bool: True if a fix was applied (raw_telegram is updated in place)
    """
    match = pattern.search(bytes(raw_telegram))
    if not match:
      return False

    full_start, full_end = match.span(0)
    original_full = match.group(0)
    value_str = match.group(1).decode(errors='replace')

    def try_candidate(candidate_full, description):
      candidate_raw = bytearray(raw_telegram)
      candidate_raw[full_start:full_end] = candidate_full
      if not verify_crc(bytes(candidate_raw), checksum_line):
        return False
      raw_telegram[full_start:full_end] = candidate_full
      logger.warning(f"Repaired {obis}: {description}; checksum now valid")
      return True

    try:
      if is_plausible(float(value_str)):
        return False  # value is fine
    except ValueError:
      # a non-digit character in the value (see the pattern's loosened digit
      # positions above, eg "0121(4.355") can't be a real reading at all --
      # fall through to the digit search below rather than giving up, since
      # that's exactly the kind of corruption it's meant to fix.
      pass

    val_rel = match.start(1) - full_start
    for pos, ch in enumerate(value_str):
      if ch == '.':
        continue
      for digit in "0123456789":
        if digit == ch:
          continue
        candidate_value = value_str[:pos] + digit + value_str[pos + 1:]
        try:
          if not is_plausible(float(candidate_value)):
            continue
        except ValueError:
          continue

        candidate_full = original_full[:val_rel] + candidate_value.encode() + original_full[val_rel + len(value_str):]
        if try_candidate(candidate_full, f"value '{value_str}' -> '{candidate_value}' "
                          f"(digit {pos}: '{ch}' -> '{digit}')"):
          return True
    return False

  def _try_repair_value_fields(self, raw_telegram, checksum_line, now):
    """
      Tries to fix an implausible cumulative-counter, instantaneous-power,
      voltage or reactive-power reading; see the module docstring.

    Returns:
      bool: True if a fix was applied
    """
    elapsed = None if self.last_reading_time is None else now - self.last_reading_time

    if elapsed is not None:
      for obis, pattern in _COUNTER_FIELD_SPECS:
        last = self.last_counter_values.get(obis)
        if last is None:
          continue
        max_delta = max(_MAX_COUNTER_POWER_KW * elapsed / 3600.0, _MIN_COUNTER_DELTA_KWH)
        if self._fix_value_field(raw_telegram, checksum_line, obis, pattern,
                                  lambda v, last=last, max_delta=max_delta: last <= v <= last + max_delta):
          return True

    for obis, pattern in _POWER_FIELD_SPECS:
      if self._fix_value_field(raw_telegram, checksum_line, obis, pattern,
                                lambda v: 0 <= v <= _MAX_INSTANT_POWER_KW):
        return True

    for obis, pattern in _VOLTAGE_FIELD_SPECS:
      if self._fix_value_field(raw_telegram, checksum_line, obis, pattern,
                                lambda v: _MIN_VOLTAGE_V <= v <= _MAX_VOLTAGE_V):
        return True

    for obis, pattern in _REACTIVE_POWER_FIELD_SPECS:
      if self._fix_value_field(raw_telegram, checksum_line, obis, pattern,
                                lambda v: 0 <= v <= _MAX_INSTANT_KVAR):
        return True

    for obis, pattern in _CURRENT_FIELD_SPECS:
      if self._fix_value_field(raw_telegram, checksum_line, obis, pattern,
                                lambda v: 0 <= v <= _MAX_CURRENT_A):
        return True

    for obis, pattern in _FREQUENCY_FIELD_SPECS:
      if self._fix_value_field(raw_telegram, checksum_line, obis, pattern,
                                lambda v: _MIN_FREQUENCY_HZ <= v <= _MAX_FREQUENCY_HZ):
        return True

    return False

  def _try_repair_glued_fields(self, raw_telegram, checksum_line):
    """
      A single field has exactly one "(" and one ")"; a line with more than
      one ")" is almost certainly two lines glued together after their
      "\\r\\n" boundary got corrupted, often along with part of the second
      field's own OBIS code (eg "1-0:61.7.0(00.000*kW)C!&KL..;.7.0(00.000*kW)",
      the ";.7.0" being what's left of a corrupted "42.7.0"). If such a
      line's start matches a known field cleanly, and the text up to the
      line's *next* ")" matches a known field's VALUE shape
      (digits/separator/unit, regardless of which OBIS code), tries every
      OBIS code that shares that shape as a candidate for the second field,
      splitting the line back in two (restoring "\\r\\n") and accepting the
      first candidate that restores the telegram's checksum.

    Returns:
      bool: True if a fix was applied (raw_telegram is updated in place)
    """
    for line_offset, line_bytes in iter_raw_lines(raw_telegram):
      if line_bytes.count(b')') < 2:
        continue

      for obis_a, pattern_a in _ALL_VALUE_FIELD_SPECS:
        match_a = pattern_a.match(line_bytes)  # field A must be right at the line's start
        if not match_a:
          continue

        gap_start = match_a.end()
        next_close = line_bytes.find(b')', gap_start)
        if next_close < 0:
          continue
        window = bytes(line_bytes[gap_start:next_close + 1])

        for shape, tail_pattern in _SHAPE_PATTERNS.items():
          tail_match = tail_pattern.search(window)
          if not tail_match or tail_match.start() == 0:
            continue  # no gap at all -- not what this is looking for

          value_bytes, sep_bytes = tail_match.group(1), tail_match.group(2)
          tail_abs_end = line_offset + gap_start + tail_match.end()

          for obis_b in _CANDIDATES_BY_SHAPE[shape]:
            if obis_b == obis_a:
              continue
            unit_b = shape[0]
            candidate_field = obis_b.encode() + b"(" + value_bytes + b"*" + unit_b.encode() + b")"
            replacement = bytes(line_bytes[:match_a.end()]) + b"\r\n" + candidate_field
            candidate_raw = (bytes(raw_telegram[:line_offset]) + replacement
                              + bytes(raw_telegram[tail_abs_end:]))
            if not verify_crc(candidate_raw, checksum_line):
              continue

            raw_telegram[:] = candidate_raw
            field_a_text = match_a.group(0).decode()
            field_b_text = candidate_field.decode()
            original_text = bytes(line_bytes).decode('utf-8', errors='replace')
            logger.warning(f"Repaired glued line: split into '{field_a_text}' and "
                            f"'{field_b_text}' (was: '{original_text}'); checksum now valid")
            return True
    return False
