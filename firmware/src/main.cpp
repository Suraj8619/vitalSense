/* VitalSense acquisition firmware — ESP32 target shell.
 *
 * This file contains no signal processing. The notch, the ring buffer, the leads-off
 * logic and the frame builder are portable C99 in vs_*.c, compiled and scored on the
 * host against MIT-BIH before they ever reach a board (D-25). What lives here is the
 * part that genuinely needs the target: pins, timing, tasks, WiFi.
 *
 * Task layout, and why:
 *
 *   core 1  ecgSampleTask   priority 5   the only real-time path in the system
 *   core 0  netTask         priority 3   WiFi and WebSocket, alongside the radio stack
 *   core 0  ppgTask         priority 2
 *   core 0  tempTask        priority 1
 *
 * Acquisition is pinned to core 1 because core 0 runs the WiFi driver, which takes
 * long, unpredictable interrupt-disabled sections. Sharing a core with it would put
 * radio scheduling directly into the sampling jitter of a medical measurement.
 *
 * Sampling is timer-driven, not polled from loop(). A loop()-paced sampler inherits
 * every delay in the program as timing error, and the DSP downstream assumes a uniform
 * 360 Hz grid — RR intervals are literally counted in samples, so jitter becomes heart
 * rate error. The timer ISR does the least possible work: it gives a semaphore. The ADC
 * read happens in the task it wakes (D-29).
 */
#include <Arduino.h>
#include <Wire.h>

/* Two transports, one acquisition path. VS_TRANSPORT_SERIAL emits the very same
 * protocol-v1 frames on the serial port instead of over a WebSocket, which is what lets
 * the firmware be run and watched in the Wokwi simulator - where there is no ingest
 * server to dial - and lets a board be exercised over USB with no network at all. The
 * frames are byte-identical either way, because both transports call the same
 * vs_frame_build(). */
#if !defined(VS_TRANSPORT_SERIAL)
#include <WebSocketsClient.h>
#include <WiFi.h>
#endif

/* The sample rate comes from the generated notch header, not from a constant defined
 * here. The device must sample at the rate its filter coefficients were designed for -
 * the server refuses any other rate for the same reason (D-14) - so the coupling is
 * made explicit rather than duplicated. */
#include "notch_coeffs.h"
#include "vs_calibration.h"
#include "vs_frame.h"
#include "vs_leadsoff.h"
#include "vs_max30102.h"
#include "vs_notch.h"
#include "vs_ringbuf.h"
#include "vs_secrets.h"

// ---------------------------------------------------------------- pins ------

static const int PIN_ECG = 36;      // AD8232 OUTPUT -> ADC1_CH0 (input-only pin)
static const int PIN_LO_PLUS = 25;  // AD8232 LO+
static const int PIN_LO_MINUS = 26; // AD8232 LO-
static const int PIN_I2C_SDA = 21;  // MAX30102 SDA
static const int PIN_I2C_SCL = 22;  // MAX30102 SCL

// ------------------------------------------------------------- constants ----

static const uint32_t SAMPLE_RATE_HZ = (uint32_t)VS_SAMPLE_RATE_HZ;
static const uint32_t FRAME_SAMPLES = 32;              // ~89 ms at 360 Hz
static const uint32_t RING_SAMPLES = 30 * SAMPLE_RATE_HZ; // SR-05: 30 s of outage

// ------------------------------------------------------------------ state ---

static hw_timer_t *sampleTimer = nullptr;
static SemaphoreHandle_t sampleReady = nullptr;

static int16_t ringStorage[RING_SAMPLES];
static vs_ringbuf_t ring;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static vs_notch_t notch;
static vs_leadsoff_t leads;

static volatile bool leadsOffLatched = true;  // start disconnected; see vs_leadsoff_init

// --- photoplethysmogram ---
// Sized for a little over one frame's worth at 100 Hz, so a late netTask does not lose
// samples but a stalled one cannot accumulate a stale backlog either.
static const uint32_t PPG_BUFFER = 64;
static vs_max30102_t ppgSensor;
static uint32_t ppgRed[PPG_BUFFER];
static uint32_t ppgIr[PPG_BUFFER];
static volatile uint32_t ppgCount = 0;
static portMUX_TYPE ppgMux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t missedSamples = 0;   // ISR fired before the task was ready

#if !defined(VS_TRANSPORT_SERIAL)
static WebSocketsClient ws;
#endif
static volatile bool linkUp = false;

// -------------------------------------------------------------- sampling ----

/* IRAM_ATTR: the handler must not live in flash. A flash read during an SPI cache miss
 * would stall the ISR for microseconds at unpredictable moments, which is exactly the
 * jitter this design exists to avoid. */
void IRAM_ATTR onSampleTimer()
{
    BaseType_t woken = pdFALSE;
    if (xSemaphoreGiveFromISR(sampleReady, &woken) != pdTRUE) {
        /* The task has not consumed the previous tick. Counting this rather than
         * ignoring it means a scheduling overrun is reportable instead of appearing
         * downstream as an unexplained gap in the waveform. */
        missedSamples++;
    }
    if (woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

static void ecgSampleTask(void *)
{
    for (;;) {
        if (xSemaphoreTake(sampleReady, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        const int32_t raw = analogRead(PIN_ECG);
        const bool loPins = digitalRead(PIN_LO_PLUS) == HIGH || digitalRead(PIN_LO_MINUS) == HIGH;

        /* Leads-off reads the RAW sample. The notch pulls a railed input back toward
         * mid-scale and would erase the saturation being detected. */
        const bool off = vs_leadsoff_update(&leads, loPins, raw);
        if (off) {
            leadsOffLatched = true;
        }

        const int32_t filtered = vs_notch_process(&notch, raw);

        portENTER_CRITICAL(&ringMux);
        vs_ringbuf_push(&ring, (int16_t)filtered);
        portEXIT_CRITICAL(&ringMux);
    }
}

// -------------------------------------------------------------- networking --

#if !defined(VS_TRANSPORT_SERIAL)
static void onWsEvent(WStype_t type, uint8_t *payload, size_t length)
{
    (void)payload;
    (void)length;
    switch (type) {
    case WStype_CONNECTED:
        linkUp = true;
        Serial.println("[net] ingest connected");
        break;
    case WStype_DISCONNECTED:
        linkUp = false;
        Serial.println("[net] ingest disconnected - buffering");
        break;
    default:
        break;
    }
}
#endif

static void netTask(void *)
{
    static char frameBuf[4096];
    static int32_t block[FRAME_SAMPLES];
    static int16_t popped[FRAME_SAMPLES];
    uint32_t seq = 0;

#if defined(VS_TRANSPORT_SERIAL)
    Serial.println("[net] serial transport - frames follow, one JSON object per line");
    linkUp = true;
#else
    WiFi.mode(WIFI_STA);
    WiFi.begin(VS_WIFI_SSID, VS_WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    Serial.printf("[net] wifi up, %s\n", WiFi.localIP().toString().c_str());

    ws.begin(VS_SERVER_HOST, VS_SERVER_PORT, VS_SERVER_PATH);
    ws.onEvent(onWsEvent);
    ws.setReconnectInterval(2000);
#endif

    for (;;) {
#if !defined(VS_TRANSPORT_SERIAL)
        ws.loop();
#endif

        /* Nothing is drained while the link is down: samples stay in the ring and go
         * out as backfill on reconnect, in the same shape as live frames (SR-05). */
        if (!linkUp) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        bool sentAny = false;
        for (;;) {
            portENTER_CRITICAL(&ringMux);
            const bool enough = vs_ringbuf_count(&ring) >= FRAME_SAMPLES;
            const uint32_t got = enough ? vs_ringbuf_pop_block(&ring, popped, FRAME_SAMPLES) : 0;
            portEXIT_CRITICAL(&ringMux);
            if (got == 0) {
                break;
            }

            for (uint32_t i = 0; i < got; ++i) {
                block[i] = popped[i];
            }

            /* Take whatever PPG has accumulated since the last frame. Nothing is held
             * back for the next one: the samples belong to the interval they were taken
             * in, and re-sending them later would misplace them in time. */
            uint32_t nPpg = 0;
            static uint32_t redOut[PPG_BUFFER];
            static uint32_t irOut[PPG_BUFFER];
            portENTER_CRITICAL(&ppgMux);
            nPpg = ppgCount;
            for (uint32_t i = 0; i < nPpg; ++i) {
                redOut[i] = ppgRed[i];
                irOut[i] = ppgIr[i];
            }
            ppgCount = 0;
            portEXIT_CRITICAL(&ppgMux);

            const vs_ppg_block_t ppgBlock = { redOut, irOut, nPpg, 100.0f };

            vs_frame_input_t in = {};
            in.device_id = VS_DEVICE_ID;
            in.seq = seq++;
            in.t_device_ms = (uint32_t)millis();
            in.fs = VS_SAMPLE_RATE_HZ;
            in.ecg_counts = block;
            in.n_samples = got;
            in.leads_off = leadsOffLatched;
            /* No PPG or temperature driver yet, so those readings are absent. Absent is
             * sent as null and never as a plausible-looking constant: a dead sensor
             * that looks healthy is the worst failure a monitor has (D-16). */
            /* Protocol v2: the device sends the light, the server derives saturation
             * where the calculation can be regression-tested (D-03). `spo2` stays absent
             * rather than being filled with a value this device did not measure. */
            in.ppg = (nPpg > 0) ? &ppgBlock : nullptr;
            in.sensors.spo2_valid = false;
            in.sensors.temp_valid = false;
            in.sensors.battery_valid = false;

            const size_t len = vs_frame_build(frameBuf, sizeof frameBuf, &in);
            if (len == 0) {
                Serial.println("[net] frame did not fit - dropped");
                continue;
            }
#if defined(VS_TRANSPORT_SERIAL)
            Serial.println(frameBuf);
#else
            ws.sendTXT(frameBuf, len);
#endif
            sentAny = true;
            leadsOffLatched = false;
        }

        vTaskDelay(pdMS_TO_TICKS(sentAny ? 1 : 5));
    }
}

// ------------------------------------------------------- other sensors ------

/* Arduino Wire behind the driver's injected transport, so vs_max30102.c itself stays
 * free of platform headers and keeps being compiled and tested on the host (D-25). */
static bool i2cRead(void *, uint8_t reg, uint8_t *dst, uint32_t len)
{
    Wire.beginTransmission(VS_MAX30102_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) {
        return false;
    }
    if (Wire.requestFrom((int)VS_MAX30102_ADDR, (int)len) != (int)len) {
        return false;
    }
    for (uint32_t i = 0; i < len; ++i) {
        dst[i] = (uint8_t)Wire.read();
    }
    return true;
}

static bool i2cWrite(void *, uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(VS_MAX30102_ADDR);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
}

static void ppgTask(void *)
{
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    Wire.setClock(400000);

    const vs_i2c_t bus = { i2cRead, i2cWrite, nullptr };
    while (!vs_max30102_init(&ppgSensor, bus)) {
        /* No sensor, or the wrong one. Frames go out with "ppg":null rather than with
         * invented light, and the retry keeps going in case it is plugged in later. */
        Serial.println("[ppg] MAX30102 not found - frames will carry ppg:null");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
    Serial.println("[ppg] MAX30102 ready, SpO2 mode at 100 Hz");

    vs_ppg_sample_t batch[VS_MAX30102_FIFO_DEPTH];
    for (;;) {
        const uint32_t got = vs_max30102_read(&ppgSensor, batch, VS_MAX30102_FIFO_DEPTH);
        if (got > 0) {
            portENTER_CRITICAL(&ppgMux);
            for (uint32_t i = 0; i < got && ppgCount < PPG_BUFFER; ++i) {
                ppgRed[ppgCount] = batch[i].red;
                ppgIr[ppgCount] = batch[i].ir;
                ppgCount++;
            }
            portEXIT_CRITICAL(&ppgMux);
        }
        /* The FIFO holds 32 samples at 100 Hz - 320 ms. Polling at 50 ms leaves a wide
         * margin without spending the bus on empty reads. */
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

static void tempTask(void *)
{
    /* DS18B20 on OneWire. Same reasoning as ppgTask. */
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void statsTask(void *)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        portENTER_CRITICAL(&ringMux);
        const uint32_t held = vs_ringbuf_count(&ring);
        const uint32_t dropped = ring.dropped;
        portEXIT_CRITICAL(&ringMux);
        Serial.printf("[stat] buffered=%u dropped=%u missedTicks=%u heap=%u leadsOff=%d "
                      "ppg=%s ppgOverflow=%u\n",
                      held, dropped, missedSamples, (unsigned)ESP.getFreeHeap(), leads.state,
                      vs_max30102_ready(&ppgSensor) ? "ok" : "absent", ppgSensor.overflow_total);
    }
}

// -------------------------------------------------------------- lifecycle ---

void setup()
{
    Serial.begin(115200);
    delay(200);
    Serial.printf("\nVitalSense acquisition - %u Hz, %u-sample frames\n",
                  (unsigned)SAMPLE_RATE_HZ, (unsigned)FRAME_SAMPLES);

    pinMode(PIN_LO_PLUS, INPUT);
    pinMode(PIN_LO_MINUS, INPUT);
    analogReadResolution(VS_ADC_BITS);
    /* 11 dB attenuation gives the full 0-3.3 V span, which is what the front end is
     * scaled to (vs_calibration.h, generated from sim/afe_model.py). */
    analogSetPinAttenuation(PIN_ECG, ADC_11db);

    vs_ringbuf_init(&ring, ringStorage, RING_SAMPLES);
    vs_notch_reset(&notch);
    vs_leadsoff_init(&leads, VS_SAMPLE_RATE_HZ);

    sampleReady = xSemaphoreCreateBinary();

    /* 80 MHz APB clock, prescaler 80 -> a 1 MHz tick, so the alarm value is simply the
     * sample period in microseconds. 360 Hz is 2777.8 us; the integer 2778 is 0.007 %
     * slow, about 0.2 s of drift per hour, which the server's arrival timestamps absorb.
     * The DSP only needs the grid to be uniform, and it is. */
    sampleTimer = timerBegin(0, 80, true);
    timerAttachInterrupt(sampleTimer, &onSampleTimer, true);
    timerAlarmWrite(sampleTimer, 1000000UL / SAMPLE_RATE_HZ, true);

    xTaskCreatePinnedToCore(ecgSampleTask, "ecg", 4096, nullptr, 5, nullptr, 1);
    xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 3, nullptr, 0);
    xTaskCreatePinnedToCore(ppgTask, "ppg", 4096, nullptr, 2, nullptr, 0);
    xTaskCreatePinnedToCore(tempTask, "temp", 2048, nullptr, 1, nullptr, 0);
    xTaskCreatePinnedToCore(statsTask, "stat", 3072, nullptr, 1, nullptr, 0);

    timerAlarmEnable(sampleTimer);
}

void loop()
{
    /* Everything runs in tasks. An empty loop() is the point: nothing time-critical is
     * allowed to depend on how long an iteration of the Arduino main loop happens to
     * take. */
    vTaskDelay(pdMS_TO_TICKS(1000));
}
