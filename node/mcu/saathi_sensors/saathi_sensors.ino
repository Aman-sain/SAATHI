/*
  SAATHI — node MCU sketch (MQ-2 gas + PIR motion + KY-037 sound)
  Owner: Divya (M2, Hardware). Area: node/** (build against the frozen contract).

  ─────────────────────────────────────────────────────────────────────────────
  HARDWARE-EVIDENCE RULE — every value below came from a label read, a command
  run, or a cited doc. Nothing here is from memory or a guess.

  BOARD  : Arduino Uno R3 — 5 V logic, 10-bit ADC (raw 0..1023).
           Confirmed by Divya off the board's printed name (2026-07-13);
           also enumerates as "Arduino Uno (COM7)" in Windows Device Manager.

  SENSOR : MQ-2 gas module — operating voltage +5 V; analog out (AO) swings 0..5 V
           with gas concentration. Cited: components101.com/sensors/mq2-gas-sensor
           (Divya's own module is the Amazon MQ-2 she linked; the retail page is
           bot-walled to tools, so a fetchable spec sheet is cited instead).

  SENSOR : PIR motion module — the board's own underside marking is "HW-416A"
           (transcribed by Divya, 2026-07-13); ordered/sold as HC-SR501. The
           HW-416 family is the same module format: pin order 5V-in / Output /
           Ground, "designed to run on 5V, internal electronics runs at 3V"
           (bdavison.napier.ac.uk/iot/Tutorials/Particle/components/pir).
           HC-SR501 spec (components101.com/sensors/hc-sr501-pir-sensor):
           input +5 V (range 4.5–12 V), output HIGH 3.3 V / LOW 0 V, H/L
           trigger jumper, allow ~1–2 min self-calibration after power-up.

  SENSOR : KY-037 sound module — 4-pin electret-mic + LM393 comparator board
           (Divya's photo 2026-07-16: 4-pin header, mic, one trim pot; silkscreen
           looks like "HW-484 V0.2" — Divya reads the labels off the unit before
           power-on). Official kit doc (sensorkit.joy-it.net/en/sensors/ky-037):
           on Arduino it connects to +5 V; AO is the "direct measured value of
           the sensor unit"; the trim pot ONLY sets the DO comparator threshold —
           it does NOT affect AO, so no knob tuning here and DO stays unused.
           Same doc: AO is INVERTED (louder → lower voltage) — irrelevant to us,
           we measure peak-to-peak SWING, which grows with loudness either way.

  VERIFIED WIRING CARD (only from the above evidence — confirm before power-on):
    MQ-2 pin   Arduino pin   Voltage / note                         Verified?
    VCC    ->  5V (+ rail)   5 V heater spec; safe on the 5 V board  [x]
    GND    ->  GND (− rail)  common ground                          [x]
    AO     ->  A0            0..5 V analog into a 0..5 V ADC — safe  [x]  (Divya, off the board)
    DO     ->  (unused)      the module's on-board comparator; we read raw AO, not DO

    PIR pin (label) wire     Arduino pin   Voltage / note           Verified?
    VCC        ->   white -> 5V (+ rail)   5 V is in-spec (4.5–12)  [x]  (Divya read VCC|OUT|GND off the module underside, 2026-07-13)
    OUT        ->   brown -> D2            module drives 3.3 V HIGH;
                                           Uno reads HIGH from 3.0 V
                                           (V_IH min = 0.6·VCC,
                                           ATmega328P datasheet §DC
                                           characteristics) — safe    [x]  (Divya recounted the header hole: D2, 2026-07-13)
    GND        ->   black -> GND (− rail)  common ground             [x]

    KY-037 pin  Arduino pin   Voltage / note                         Verified?
    +       ->  5V (+ rail)   5 V per the joy-it Arduino hookup       [ ] Divya reads the label
    G       ->  GND (− rail)  common ground                           [ ] Divya reads the label
    AO      ->  A1            mic level around a DC bias, ≤5 V into
                              a 0..5 V ADC — safe on this 5 V board   [ ] Divya reads the label
    DO      ->  (unused)      comparator out; we read AO (pot only
                              moves the DO threshold, never AO)
    ⚠ WIRE BY LABEL, NOT POSITION — variants of this module shuffle the pin
      order. If the four labels aren't AO / G / + / DO, stop and re-check.
    Sound-only bench (MQ-2 + PIR unplugged): park A0→GND and D2→GND with two
    jumpers, else both inputs float and invent garbage gas/motion readings.

  WHY THIS IS SAFE HERE: a 5 V AO into a 5 V board's ADC is within range, and the
  PIR's 3.3 V OUT is an in-range logic HIGH for a 5 V input pin.
  ⚠ It would NOT be safe on a 3.3 V board (e.g. Arduino UNO Q): a ~5 V AO exceeds
    a ~3.6 V ADC limit and needs a voltage divider FIRST — that applies to BOTH
    analog outs, the MQ-2's AND the KY-037's. (The PIR OUT at 3.3 V would be fine
    there.) If the board is ever swapped to a 3.3 V part → STOP and add the
    dividers before wiring any AO.

  ─────────────────────────────────────────────────────────────────────────────
  SERIAL OUTPUT CONTRACT — this MUST stay line-for-line compatible with the
  publisher's --serial parser, node/linux/publisher.py:
        RAW_RE = re.compile(r"MQ-2 raw:\s*(\d+)")     # matched with .search()
        PIR_RE = re.compile(r"PIR:\s*([01])")         # matched with .search()
        SND_RE = re.compile(r"SND:\s*(\d+)")          # matched with .search()
  So every data line is exactly:   MQ-2 raw: <n> | PIR: <p> | SND: <s>
  (<n> = integer 0..1023, <p> = 0 or 1, <s> = mic peak-to-peak 0..1023 over the
  ~480 ms listen window) at 9600 baud — one line = one atomic snapshot of all
  three sensors. Because the regexes use .search(), an OLDER publisher still
  parses gas/PIR from this line, and older sketch lines (gas-only, gas+PIR)
  still feed THIS parser — missing fields just stay at their placeholders.
  Do NOT change the tokens, the colons, or the baud without also changing
  publisher.py. The one-time banner below deliberately avoids the tokens
  "MQ-2 raw:", "PIR:" and "SND:" so the parser never mistakes it for a reading.

  SAFE TESTING: no open flame indoors. Trigger with butane from an UNLIT lighter
  held near the sensor, or an alcohol/marker whiff. Let it warm up first — the raw
  value starts high, settles to a clean-air baseline (~95 on this rig), then climbs
  on gas and falls as it clears.
*/

const int MQ2_AO = A0;    // CONFIRMED off the board by Divya (2026-07-13): MQ-2 AO -> A0
const int PIR_OUT = 2;    // CONFIRMED off the board by Divya (2026-07-13): PIR OUT (brown) -> D2
const int SND_AO = A1;    // KY-037 AO -> A1 (A0 stays the MQ-2's) — Divya confirms the label at wiring time

void setup() {
  Serial.begin(9600);     // MUST be 9600 — matches publisher.py --serial (default --baud 9600)
  pinMode(PIR_OUT, INPUT);  // module actively drives OUT high/low — no pull-up wanted
  // One-time banner. NOTE: no "MQ-2 raw:"/"PIR:" tokens here, so publisher.py skips it.
  Serial.println(F("SAATHI node up: MQ-2 gas on A0 + PIR motion on D2 + KY-037 sound on A1, Uno R3, 9600 baud"));
  Serial.println(F("warming up... MQ-2 settles over minutes; PIR self-calibrates ~1-2 min (ignore its pulses until then)"));
}

void loop() {
  // A clap is a burst only a few ms long — a single analogRead per line would
  // miss almost every one. So the mic is sampled for the WHOLE ~480 ms window
  // (this replaces the old delay(500) as the loop's pacing) and loudness is
  // reported as the peak-to-peak swing (max−min) around the module's DC bias.
  analogRead(SND_AO);             // discard one read after switching ADC channel
  int mn = 1023, mx = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < 480) {
    int s = analogRead(SND_AO);
    if (s < mn) mn = s;
    if (s > mx) mx = s;
  }
  analogRead(MQ2_AO);             // discard one read after switching ADC channel
  int raw = analogRead(MQ2_AO);   // 0..1023 (10-bit, 5 V reference)
  int pir = digitalRead(PIR_OUT); // 1 = module holds OUT high (motion), 0 = idle
  Serial.print(F("MQ-2 raw: "));  // exact token publisher.py RAW_RE looks for
  Serial.print(raw);              // integer only — no decimals, no sign
  Serial.print(F(" | PIR: "));    // exact token publisher.py PIR_RE looks for
  Serial.print(pir);              // 0 or 1 only
  Serial.print(F(" | SND: "));    // exact token publisher.py SND_RE looks for
  Serial.println(mx - mn);        // mic peak-to-peak over the window, 0..1023
  // no delay() here — the 480 ms listen window paces the ~2 lines/second cadence
}
