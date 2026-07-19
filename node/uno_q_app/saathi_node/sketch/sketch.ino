#include "Arduino_RouterBridge.h"

// ---- pins (verified wiring, 2026-07-19, multimeter-checked) ----
const int GAS_AO  = A0;  // MQ-2 AO via 10k+10k divider (tap measured 1.8V max — safe)
const int SND_AO  = A1;  // KY-037 AO via its own 10k+10k divider
const int PIR_OUT = 2;   // PIR OUT (3.3V logic) — direct
const int DHT_PIN = 4;   // DHT11 DATA (powered from 3.3V) — direct

// Latest readings. loop() refreshes these continuously;
// the Bridge functions below just hand back the cached value instantly.
volatile int g_gas    = 0;    // raw 0..16383 (UNO Q ADC is 14-bit)
volatile int g_snd_pp = 0;    // mic peak-to-peak over the last window
volatile int g_pir    = 0;    // 1 = motion seen during the last window
volatile int g_temp   = -99;  // whole °C; -99 = no valid DHT11 read yet

// ---- functions Python can call over the Bridge ----
int read_gas()   { return g_gas; }
int read_sound() { return g_snd_pp; }
int read_pir()   { return g_pir; }
int read_temp()  { return g_temp; }

// ---- DHT11 one-wire reader (no library needed) ----
// Protocol: we pull the line LOW 20ms to say "talk to me", then the sensor
// sends 40 bits; a bit is 0 or 1 depending on how LONG its HIGH pulse is.
// ---- DHT11 one-wire reader with failure codes ----
// -91 = no response (wiring/power)   -92 = bits stopped (timing)
// -93 = checksum bad (corrupted)     >=0 = valid °C
int dht11_read_c() {
  uint8_t d[5] = {0, 0, 0, 0, 0};
  pinMode(DHT_PIN, OUTPUT);
  digitalWrite(DHT_PIN, LOW);
  delay(20);                       // start signal (>=18 ms)
  pinMode(DHT_PIN, INPUT_PULLUP);  // release the line, sensor takes over
  if (pulseIn(DHT_PIN, HIGH, 1000) == 0) return -91;   // no "hello" from sensor
  for (int i = 0; i < 40; i++) {                        // 40 data bits
    unsigned long t = pulseIn(DHT_PIN, HIGH, 1000);
    if (t == 0) return -92;
    d[i / 8] <<= 1;
    if (t > 45) d[i / 8] |= 1;
  }
  if ((uint8_t)(d[0] + d[1] + d[2] + d[3]) != d[4]) return -93;
  return d[2];
}

void setup() {
  pinMode(GAS_AO, INPUT);
  pinMode(SND_AO, INPUT);
  pinMode(PIR_OUT, INPUT);         // module drives OUT itself — no pull-up
  Bridge.begin();                  // NOTE: we never touch Serial1 — router owns it
  Bridge.provide_safe("read_gas",   read_gas);
  Bridge.provide_safe("read_sound", read_sound);
  Bridge.provide_safe("read_pir",   read_pir);
  Bridge.provide_safe("read_temp",  read_temp);
}

void loop() {
  // Sound: a clap is only a few ms long — one read would miss it. So we
  // listen for a whole 480 ms window and keep the swing (max - min).
  // PIR is checked all through the window too, so a short pulse latches.
  analogRead(SND_AO);              // discard one read after switching channel
  int mn = 16383, mx = 0, pir = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < 480) {
    int s = analogRead(SND_AO);
    if (s < mn) mn = s;
    if (s > mx) mx = s;
    if (digitalRead(PIR_OUT)) pir = 1;
  }
  g_snd_pp = mx - mn;
  g_pir = pir;

  analogRead(GAS_AO);              // discard one read after switching channel
  g_gas = analogRead(GAS_AO);      // 0..16383

  // DHT11 is slow (max ~1 read/sec) — read it every ~5 windows,
  // and only overwrite on a GOOD read (a failed read keeps the last value).
  static int tick = 0;
  if (++tick >= 5) {
    tick = 0;
    int t = dht11_read_c();
    if (t != -99) g_temp = t;
  }
}
