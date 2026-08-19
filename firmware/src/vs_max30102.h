/* MAX30102 pulse-oximeter driver: register access, configuration, FIFO decoding.
 *
 * Portable C99 with the I2C transport injected, so the whole driver except the two
 * lines that actually touch the bus is compiled and exercised on the host against a
 * simulated device (D-25). Register sequences, 18-bit sample decoding, FIFO wrap-around
 * and overflow accounting are all logic, and logic does not need a chip to be wrong.
 *
 * The part is configured for SpO2 mode at 100 Hz, which is one of the rates PPG filter
 * coefficients are generated for - the server refuses any other (D-14). In this mode the
 * FIFO holds six bytes per sample: three for the red LED, then three for infrared, each
 * an 18-bit count left-padded into 24 bits.
 *
 * What the driver deliberately does NOT do: compute a saturation. The device sends raw
 * red and infrared light and the server derives SpO2 from it, where the calculation can
 * be regression-tested against recordings (protocol v2, D-03).
 */
#ifndef VS_MAX30102_H
#define VS_MAX30102_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VS_MAX30102_ADDR      0x57
#define VS_MAX30102_PART_ID   0x15
/** Hardware FIFO depth, in samples. Beyond this the part overwrites and counts. */
#define VS_MAX30102_FIFO_DEPTH 32

/**
 * I2C transport. Supplied by the caller so the driver has no platform dependency:
 * the ESP32 build passes Wire, the host tests pass a simulated device.
 *
 * Both callbacks return false on a bus error. A driver that cannot tell a failed read
 * from a zero reading would turn a dead sensor into a plausible signal, which is the
 * failure D-16 exists to prevent.
 */
typedef struct {
    bool (*read)(void *ctx, uint8_t reg, uint8_t *dst, uint32_t len);
    bool (*write)(void *ctx, uint8_t reg, uint8_t value);
    void *ctx;
} vs_i2c_t;

typedef struct {
    vs_i2c_t bus;
    bool ready;
    uint32_t overflow_total; /* samples the part dropped before we read them */
    uint32_t bus_errors;
} vs_max30102_t;

/** One FIFO sample: raw counts, red and infrared, sampled at the same instant. */
typedef struct {
    uint32_t red;
    uint32_t ir;
} vs_ppg_sample_t;

/**
 * Reset, verify the part ID, and configure SpO2 mode at 100 Hz.
 *
 * Returns false if the bus fails or the part identifies as something else - streaming
 * from an unknown chip and hoping the register map matches is how a monitor ends up
 * reporting numbers derived from the wrong bytes.
 */
bool vs_max30102_init(vs_max30102_t *dev, vs_i2c_t bus);

/** True when init succeeded and the part is configured. */
bool vs_max30102_ready(const vs_max30102_t *dev);

/**
 * Drain up to `max` samples from the FIFO into `out`.
 *
 * Returns the number of samples read; 0 means the FIFO was empty, which is normal.
 * Overflow is accumulated in `dev->overflow_total` rather than reported as an error:
 * losing samples matters and must be counted, but it does not invalidate the ones that
 * did arrive (the same argument as the ring buffer's dropped counter, D-27).
 */
uint32_t vs_max30102_read(vs_max30102_t *dev, vs_ppg_sample_t *out, uint32_t max);

/** Decode one 3-byte, 18-bit FIFO channel value. Exposed for testing. */
uint32_t vs_max30102_decode(const uint8_t *three_bytes);

#ifdef __cplusplus
}
#endif

#endif /* VS_MAX30102_H */
