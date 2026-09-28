# DSMR telegram repair — specification

This describes how `dsmr_repair.py` (used by `P1_serial.py`) validates DSMR P1
telegrams and repairs ones that were damaged by noise on the P1 line.

## 1. Goal and safety rule

A telegram is published only if its DSMR CRC-16 matches. If it doesn't match,
the repairer tries a fixed set of targeted corrections. **A correction is
accepted only if it makes the CRC match.** A wrong fix that also happens to
produce the right CRC is roughly a 1-in-65536 event. If no correction
restores the CRC, the telegram is discarded rather than published with a
guessed value.

The exceptions are the normalization steps (sections 5.0a–5.0d).
They are applied without their own CRC check, but they never make a
telegram acceptable by themselves: the telegram still has to pass the CRC
afterwards.

**Every fix is made on the raw bytes only.** The decoded lines that get
published are built from the accepted raw bytes afterwards, so what is
published is always exactly what the CRC verified.

## 2. Checksum

- Algorithm: CRC-16/ARC (reflected polynomial `0xA001`, initial value `0x0000`).
- Coverage: every raw byte from the leading `/` of the header through the `!`
  of the final line, including all `\r\n` line terminators.
- Expected value: the 4 hex digits after `!` on the final line (`!XXXX`,
  case-insensitive).
- A final line without 4 hex digits after `!` counts as a failed check.

The CRC is always computed on the raw bytes: as received, or after a
repair.

## 3. Processing pipeline (P1_serial)

For each telegram:

1. **Sync.** Discard lines until one starts with `/` (the header).
2. **Read.** Collect raw bytes until a line starting with `!`. No decoded
   lines are built yet.
3. **Size cap.** If more than 1600 raw bytes are read without reaching a `!`
   line, the read is treated as two or more telegrams merged by a corrupted
   terminator:
   - Search the buffer for a second copy of this read's own header line.
   - If one is found, save the bytes before it as a `_leadin` dump and
     discard them. Continue reading the embedded telegram up to its own `!`
     line (still subject to the 1600-byte cap), then treat it as a normal
     telegram from step 4.
   - Otherwise discard the whole read and save it as a dump.
4. **Validate.** Compute the CRC over the raw bytes through `!`.
5. **Repair.** If the CRC fails, keep a copy of the bytes as received, then
   call `TelegramRepairer.try_repair()` (section 5), which changes the raw
   bytes in place. If it fails, discard the telegram and save the copy as a
   dump (section 7).
6. **Learn.** For every accepted telegram (valid or repaired), call
   `update_baseline()` (section 4).
7. **Decode.** Build the telegram list from the accepted raw bytes with
   `decode_lines()`: the counter, then the header, the blank line and every
   field line, without the `!XXXX` line.
8. **Publish.** Add the virtual average-power fields, write
   `/mnt/ramdisk/p1_live.txt` and signal the MQTT side.

## 4. Baseline (learned from accepted telegrams)

`update_baseline()` records the following from the latest accepted telegram:

| Item | Source | Used by |
|---|---|---|
| `last_header` | first line (e.g. `/ESO5\253880853_A`) | header normalization |
| `last_counter_values` | values of kWh/kvarh registers | counter plausibility |
| `last_equipment_id` | `0-0:96.1.1(...)` value | equipment id (fallback) |
| `last_text_message` | `0-0:96.13.0(...)` value | text message (fallback) |
| `last_field_lines` | every field line, in order (the header, the blank line after it and the `!` line are skipped) | template normalization |
| `field_sequence` | the OBIS code of each of those lines | OBIS-by-position repair |
| `recent_field_lines` | the field lines of the last 10 accepted telegrams | stable-digit repair |
| `last_reading_time` | `time.time()` at acceptance | counter plausibility |

Right after startup the baseline is empty. Until the first valid telegram
arrives, only the steps that don't depend on it can succeed: framing
characters, OBIS codes (one-character rule), value formats using the
hard-coded units, OBIS prefix, power/voltage/kvar value fields and glued
fields.

## 5. Repair steps

`try_repair()` first runs all the normalizations (5.0a–5.0d), which can fix
any number of places at once. If any of them changed anything and the CRC now
matches, the repair is done. Otherwise it runs the CRC-gated repairs
(5.1–5.6) on the normalized bytes, in order, and **stops at the first one
that restores the CRC**. So errors of the normalization kinds plus one other
error can all be fixed, but two independent errors of the other kinds (e.g.
two bad digits) cannot.

Each step edits only the raw bytes. The published lines are built from them
afterwards (section 3, step 7).

**Final check: well-formed.** A repaired telegram is only accepted if,
besides matching the CRC, it is well-formed:
- **Line count and codes:** it has as many field lines as the last valid
  telegram, each with the expected OBIS code in the expected order. Without
  a last valid telegram, every code must be a known code (section 6).
- **Values:** every value with a known format fits it exactly: digits at
  digit positions, `S`/`W` at the timestamp's flag, and every other
  character as in the format. Any other value must be hex digits followed
  by `)`.

The reason: the CRC-gated repairs try many candidates, so across thousands
of noisy telegrams one occasionally matches the CRC by pure chance (1 in
65536 each). This has been observed: a telegram with two garbage lines was
"repaired" by an unrelated change of `1-0:4.8.0` into `1-0:3.8.0`. A real
repair always produces a well-formed telegram; a chance match practically
never does.

The normalizations are not CRC-gated themselves. Each one only changes
something that is certainly corrupt (a character that can't appear, an
unknown code, or a value that differs from a field that never changes), and
the telegram is still only accepted if the full CRC passes afterwards.

### 5.0a Framing characters

- **Scope:** the whole telegram.
- **Detect:** any `<` or `>` byte. Neither can appear in a valid DSMR
  telegram.
- **Fix:** replace every `<` with `(` and every `>` with `*`, e.g.
  `1-0:41.7.0<00.000*kW)` → `1-0:41.7.0(00.000*kW)`.
- **Log:** each affected line is logged at INFO level before the change.
- **If `<`/`>` replaced a digit** (e.g. `00.09<`): the value then contains
  `(` or `*`, isn't a number, and is repaired by the digit search in 5.5.

### 5.0b Header

- **Requires:** `last_header`, the header from the last telegram that passed
  its CRC.
- **Detect:** the first line differs from `last_header`.
- **Fix:** replace the whole first line with `last_header`, even if the
  lengths differ, e.g. `/ES?5\253880853_A` → `/ESO5\253880853_A`.

### 5.0c All field lines from the last valid telegram (template)

**Glued lines are split first.** When the telegram has fewer field lines
than the last valid one, `\r\n` line breaks were lost, gluing lines
together. Usually the end of the first field and the start of the second
are lost with them, e.g. `1-0:3.7.0(00.512*-W..%C!nK\...7.0(00.098*kvar)`.
- **Detect:** walk the lines alongside the template. A line longer than its
  template line by more than half of the next template line is two lines
  glued together.
- **Split:** keep its first `len(template line)` bytes and its last
  `len(next template line)` bytes, with `\r\n` between them. Whatever was in
  between is noise and is dropped; the two parts may overlap if bytes were
  lost.
- **Then:** the line count matches again, and the rest of this step fixes
  both halves' codes and formats, e.g. → `1-0:3.7.0(00.512*kvar)` +
  `1-0:4.7.0(00.098*kvar)`.

- **Used when:** the telegram has exactly as many field lines as the last
  valid one (`last_field_lines`). This meter always sends the same lines in
  the same order and format, so each line has a template P: the same line of
  the last valid telegram. Otherwise 5.0d is used instead.
- **All lines are fixed in one pass.**

**OBIS code and `(`.** P's code is expected, and so is its length. The line's
own code is located and replaced by P's code:

| Line looks like | Meaning | Fix | Example |
|---|---|---|---|
| any `len(code)` characters, then `(` | the code is corrupted, or is a different known code | replace those characters | `LKL\x1d\x1b3.7.0(` → `1-0:63.7.0(`; `1-0:4.(.0(` → `1-0:4.8.0(` (a `<` in the code became `(` in 5.0a); `1-0:1.8.1(` → `1-0:1.8.2(` |
| the code, one other byte, then `(` | a byte was inserted | remove that byte | `1-0:52.36.0\x00(` → `1-0:52.36.0(` |
| the code, then something other than `(` | the `(` is corrupted | put back `(` | `1-0:52.7.0&226.6` → `1-0:52.7.0(226.6` |
| exactly as long as P | code and `(` are corrupted but in place | replace the first `len(code) + 1` characters | `1-0:1.8.&'LL\x18\x1800.000*kWh)` → `1-0:1.8.3(LL\x18\x1800.000*kWh)` (the digits are then fixed by 5.1) |
| anything else with a `(` | a character was lost or inserted in the code | replace everything before the first `(` | `1-0:2.7.0(` on the `1-0:21.7.0` line → `1-0:21.7.0(` |
| no `(` at all | can't be located | line left alone | |

**Value**, i.e. everything after the `(`, including the closing `)`:
- **Fields that practically never change:** copied whole from P. These are
  the equipment id `0-0:96.1.1`, the text message `0-0:96.13.0`, the number
  of power failures `0-0:96.7.21`, and the sag/swell counters
  `1-0:{32,52,72}.32.0` and `1-0:{32,52,72}.36.0`. For example,
  `1-0:32.36.0(00080)` → `1-0:32.36.0(00000)`. A real change arrives in a
  telegram with a valid CRC, which never reaches the repairer, and becomes
  the new template. If a real change happens in the same second as
  corruption elsewhere, that one telegram is lost.
- **Fields with a known format (section 6), i.e. all other fields of this
  meter:** the fixed characters are put back from the format, independent of
  P's value. So a power factor that changed sign since P is handled too. See
  "Value formats" in section 6.
- **A field without a known format** (none on this meter today): if the value
  has the same length as P's, every position where P has a non-digit is
  copied from P. Otherwise it is left as is.

### 5.0d Fallback when the line count differs

Used right after startup, or when a line was lost or two lines were glued
together. Each of these rules looks for its field anywhere in the telegram.

- **OBIS codes:** a code before the first `(` that isn't a known code
  (section 6) is replaced by the one known code of the same length that
  differs in a single character, e.g. `1-0:62.7?0` → `1-0:62.7.0`. A code
  that is one character from several known codes (e.g. `1-0:1.8.5`) is left
  alone. This runs first, because the patterns below are anchored on the
  OBIS code.
- **Equipment id and text message:** the value between `OBIS(` and `)` is
  replaced with `last_equipment_id` / `last_text_message` if it differs, even
  if the lengths differ.
- **Value format:** for every line whose code (before the first `(`) has a
  known format, the value's fixed characters are put back, exactly as in
  5.0c (see "Value formats" in section 6).

### 5.1 Non-digit in a digit position (CRC-gated)

- **Scope:** every field with a known format (section 6, "Value formats").
  The format says exactly where the digits are.
- **Detect:** a character that isn't `0`–`9` at a digit position, e.g. the
  timestamp `2609;3222639S`. That character is certainly corrupt.
- **Candidates:**
  - **Exactly one such position in the whole telegram:** the digits `0`–`9`
    at that position.
  - **Several positions:** a single candidate, with each position filled in
    with the digit the last valid telegram had there. This needs the same
    line count as that telegram. It is right for values that rarely change,
    e.g. an unused tariff's `000000.000` (`LL\x18\x1800.000` →
    `000000.000`), a counter's high digits, or a voltage's first digits.
- **Why not all combinations for several positions:** there would be 100 or
  more candidates, and the chance that a wrong one passes the CRC by
  accident (about 0.15% for 100) is too high.

### 5.2 Stable digit changed (CRC-gated)

- **Requires:** the last 10 accepted telegrams (`recent_field_lines`), all
  with the same line count as this one.
- **Stable digit:** a digit position that had the same digit in each of
  those 10 telegrams, e.g. a phase's power that is always `00.000`, an unused
  tariff, or the high digits of a counter or a voltage.
- **Detect:** 1–3 stable digits that differ in this telegram, e.g.
  `1-0:62.7.0(00.008*kW)` where every other telegram had `00.000`. The value
  looks plausible and well-formed, so nothing else can spot it.
- **Candidates:** each of them reverted alone, then all of them reverted
  together. That's at most 4 candidates.
- **Why not every digit:** trying every digit is not safe. For a single
  telegram, several different single-digit changes can match the CRC (three
  in the telegram above). More than 3 changed stable digits is not tried.

### 5.3 OBIS code, one wrong character (CRC-gated)

- **Scope:** every line that has a `(` after at least one character; the text
  before the first `(` is the OBIS prefix.
- **Detect:** the prefix is the same length as a known OBIS code (section 6)
  and differs from it in exactly one character.
- **Candidates:** each such known code, one at a time, e.g.
  `1-0:1.8.1` → `1-0:1.8.2`.
- **Catches:** mostly what 5.0c leaves alone: a corrupted code that is itself
  a known code, or is one character from several codes.

### 5.4 OBIS code by position (CRC-gated)

- **Requires:** `field_sequence`.
- **Detect:** the field line at position *n* (counting from the first field
  after the blank line) has a prefix different from `field_sequence[n]`.
- **Candidate:** replace the prefix with `field_sequence[n]`. The lengths may
  differ.

### 5.5 Value fields (CRC-gated)

Fields are tried in this order: counters, then active power, voltage,
reactive power, current and frequency. For each field that matches its loose pattern:

- If the value isn't plausible, or isn't a number at all (e.g. `0121(4.355`
  after 5.0a), try every single-position substitution with a digit `0`–`9`
  (the decimal point is never changed). Only candidates that are plausible
  are CRC-tested.
- The decimal point, separator and unit are already correct at this point
  (5.0c or 5.0d).

Plausibility rules:

| Field type | Unit | Rule |
|---|---|---|
| Cumulative counter | kWh, kvarh | `last ≤ v ≤ last + max(50 kW × elapsed_s / 3600, 0.01)`, which needs a baseline and `last_reading_time` |
| Active power | kW | `0 ≤ v ≤ 40` |
| Phase voltage | V | `150 ≤ v ≤ 280` |
| Reactive power | kvar | `0 ≤ v ≤ 40` |
| Phase current | A | `0 ≤ v ≤ 100` |
| Frequency | Hz | `45 ≤ v ≤ 55` |

### 5.6 Glued fields, lost line break (CRC-gated)

- **Detect:** a line containing two or more `)` characters, which starts with
  a cleanly matching known value field A.
- **Tail:** in the text between the end of A and the next `)`, find a
  value+unit that has the exact shape of some known field (digits, any
  separator, the literal unit, `)`), and isn't right at the start of that
  text.
- **Candidates:** for every OBIS code B with that shape (B ≠ A), rebuild the
  text as `A\r\nB(value*unit)`. Whatever came between A and the value is
  dropped.
- **Result:** on success, the raw line becomes two lines, so two decoded
  lines are published.
- **Example:**
  `1-0:61.7.0(00.000*kW)C!&KL..;.7.0(00.000*kW)` →
  `1-0:61.7.0(00.000*kW)` + `1-0:42.7.0(00.000*kW)`.

## 6. Known fields

Known OBIS codes, used by 5.0d and 5.3:
`0-0:1.0.0`, `1-0:1.8.0`–`1-0:1.8.4`, `1-0:2.8.0`, `1-0:3.8.0`, `1-0:4.8.0`,
`1-0:1.7.0`–`1-0:4.7.0`, `1-0:{21,41,61,22,42,62,23,43,63,24,44,64}.7.0`,
`1-0:{32,31,52,51,72,71,14,13,33,53,73}.7.0`, `0-0:96.7.21`, `0-0:96.13.0`,
`1-0:{32,52,72}.32.0`, `1-0:{32,52,72}.36.0`, `0-0:96.1.1`.

Value fields with a plausibility rule, used by 5.5 and 5.6. The format is
digits before and after the decimal point (0 after means no decimal point),
then `*` and the unit:

| OBIS | Unit | Format |
|---|---|---|
| `1-0:1.8.0`–`1-0:1.8.4`, `1-0:2.8.0` | kWh | 6.3 |
| `1-0:3.8.0`, `1-0:4.8.0` | kvarh | 6.3 |
| `1-0:1.7.0`, `1-0:2.7.0`, `1-0:{21,41,61,22,42,62}.7.0` | kW | 2.3 |
| `1-0:{32,52,72}.7.0` | V | 3.1 |
| `1-0:3.7.0`, `1-0:4.7.0`, `1-0:{23,43,63,24,44,64}.7.0` | kvar | 2.3 |
| `1-0:{31,51,71}.7.0` | A | 3.0 |
| `1-0:14.7.0` | Hz | 2.1 |

### Value formats

Every field's value (everything after the `(`) has a fixed format. In the
format, `0` marks a digit and `S` marks the timestamp's summer/winter flag.
Every other character is always exactly that character:

| Fields | Format |
|---|---|
| kWh / kvarh counters | `000000.000*kWh)` / `000000.000*kvarh)` |
| kW / kvar | `00.000*kW)` / `00.000*kvar)` |
| V / A / Hz | `000.0*V)` / `000*A)` / `00.0*Hz)` |
| power factors `1-0:{13,33,53,73}.7.0` | `0.000)`, optionally preceded by `-` |
| timestamp `0-0:1.0.0` | `000000000000S)` |

Applying a format (5.0c, 5.0d):
- **Length check:** the value must have the format's length, or for a power
  factor the format's length + 1. Otherwise it is left alone.
- **Power factor sign:** a value one character longer than the format starts
  with `-`, so its first character is set to `-`.
- **Digit positions:** the received character is kept, even if it isn't a
  digit. A wrong digit is left to 5.5.
- **`S` position:** `S` or `W` is kept. Anything else is replaced by the
  template's character, or `S` without a template.
- **Every other position:** set to the format's character. For example,
  `0?590)` → `0.590)`, `00?000*kW)` → `00.000*kW)`, `000%A)` → `000*A)`,
  `50,0*Hx)` → `50.0*Hz)`.

Other patterns:
- **Loose field pattern (5.5, 5.6, baseline):** `OBIS(` + any N chars + `.` +
  any M chars + any 1 char (the separator) + any `len(unit)` chars + `)`. The
  OBIS code, the parentheses and the decimal point are literal.
- **Shape-only pattern (5.6):** real digits, the decimal point and the
  literal unit, without an OBIS code.

Digits that are **never** repaired: those of the timestamp `0-0:1.0.0` and
of the power factors `1-0:{13,33,53,73}.7.0`. There is no plausibility rule
for them. Their fixed characters are repaired by 5.0c/5.0d.

## 7. Failed telegram dumps

**Disabled by default** (`_SAVE_FAILED_TELEGRAMS = False` in `P1_serial.py`) to save space. Set it to `True` and restart the service to collect dumps again, e.g. to look for new corruption patterns. When enabled:

- **Location:** `/mnt/ramdisk/dsmr_failed_telegrams/` (tmpfs). Only the 500
  newest files are kept.
- **Name:** `YYYYmmdd-HHMMSS_<counter>[_leadin].raw`.
- **Content:** the telegram bytes as received (before any normalization or
  repair) through `!`, followed by the 4 checksum hex digits when a checksum
  line was found. **CR bytes are removed.**
- **To re-verify a dump:** first turn every `\n` back into `\r\n`.

## 8. Known limitations

- Only one CRC-gated repair per telegram (after the normalizations).
- Most normalization only works when the telegram has as many field lines
  as the last valid one (5.0c). With a lost or glued line, only the weaker
  per-field fallback (5.0d) applies.
- 5.0a replaces every `<`/`>`. Inside an OBIS code, the resulting `(` is
  handled by 5.0c, but not by the fallback 5.0d.
- Inserted bytes (often NUL) are only removed right after an OBIS code
  (5.0c). Elsewhere, e.g. inside a value, they change the value's length and
  make the telegram unrepairable.
- Removing CRs from dumps loses a corruption that hit a CR byte.
- The "embedded header" recovery (section 3, step 3) only searches for the
  current read's own first line. If that line is itself corrupted, the
  recovery fails.
