#include "vs_max30102.h"

#include <string.h>

/* Register map, from the MAX30102 datasheet. */
#define REG_INTR_STATUS_1 0x00
#define REG_INTR_ENABLE_1 0x02
#define REG_INTR_ENABLE_2 0x03
#define REG_FIFO_WR_PTR   0x04
#define REG_OVF_COUNTER   0x05
#define REG_FIFO_RD_PTR   0x06
#define REG_FIFO_DATA     0x07
#define REG_FIFO_CONFIG   0x08
#define REG_MODE_CONFIG   0x09
#define REG_SPO2_CONFIG   0x0A
#define REG_LED1_PA       0x0C /* red */
#define REG_LED2_PA       0x0D /* infrared */
#define REG_PART_ID       0xFF

#define MODE_RESET        0x40
#define MODE_SPO2         0x03

/* ADC range 4096 nA (01), sample rate 100 Hz (011), pulse width 411 us / 18-bit (11).
 * 100 Hz because that is a rate the PPG filter coefficients are generated for; 18-bit
 * because the pulsatile component is a fraction of a percent of the DC level, and
 * throwing away resolution there throws away the signal. */
#define SPO2_CONFIG_VALUE ((0x01u << 5) | (0x03u << 2) | 0x03u)

/* Sample averaging off, FIFO rollover enabled, interrupt when 17 samples remain. */
#define FIFO_CONFIG_VALUE 0x0F

/* Bytes per sample in SpO2 mode: three for red, three for infrared. */
#define BYTES_PER_SAMPLE 6

static bool write_reg(vs_max30102_t *dev, uint8_t reg, uint8_t value)
{
    if (!dev->bus.write(dev->bus.ctx, reg, value)) {
        dev->bus_errors++;
        return false;
    }
    return true;
}

static bool read_reg(vs_max30102_t *dev, uint8_t reg, uint8_t *dst, uint32_t len)
{
    if (!dev->bus.read(dev->bus.ctx, reg, dst, len)) {
        dev->bus_errors++;
        return false;
    }
    return true;
}

uint32_t vs_max30102_decode(const uint8_t *three_bytes)
{
    /* 18 bits, left-padded into 24. The top six bits are undefined and must be masked:
     * leaving them in would add up to 258048 counts of garbage to a reading whose whole
     * pulsatile range is a few thousand. */
    const uint32_t raw = ((uint32_t)three_bytes[0] << 16) | ((uint32_t)three_bytes[1] << 8) |
                         (uint32_t)three_bytes[2];
    return raw & 0x0003FFFFu;
}

bool vs_max30102_init(vs_max30102_t *dev, vs_i2c_t bus)
{
    if (dev == NULL || bus.read == NULL || bus.write == NULL) {
        return false;
    }
    memset(dev, 0, sizeof(*dev));
    dev->bus = bus;
    dev->ready = false;

    uint8_t part = 0;
    if (!read_reg(dev, REG_PART_ID, &part, 1)) {
        return false;
    }
    if (part != VS_MAX30102_PART_ID) {
        /* Something is on the bus, but it is not this part. Its register map is
         * unknown, so every subsequent read would be a guess dressed as a measurement. */
        return false;
    }

    if (!write_reg(dev, REG_MODE_CONFIG, MODE_RESET)) return false;

    /* Clear both FIFO pointers so the first read starts from a known position rather
     * than from whatever the part held before the reset. */
    if (!write_reg(dev, REG_FIFO_WR_PTR, 0)) return false;
    if (!write_reg(dev, REG_OVF_COUNTER, 0)) return false;
    if (!write_reg(dev, REG_FIFO_RD_PTR, 0)) return false;

    if (!write_reg(dev, REG_FIFO_CONFIG, FIFO_CONFIG_VALUE)) return false;
    if (!write_reg(dev, REG_SPO2_CONFIG, SPO2_CONFIG_VALUE)) return false;
    /* LED currents. Equal drive on both, ~6.4 mA, a middle setting: too low and the
     * pulsatile component sinks into the noise, too high and the detector saturates on
     * a thin finger. */
    if (!write_reg(dev, REG_LED1_PA, 0x24)) return false;
    if (!write_reg(dev, REG_LED2_PA, 0x24)) return false;
    if (!write_reg(dev, REG_MODE_CONFIG, MODE_SPO2)) return false;

    dev->ready = true;
    return true;
}

bool vs_max30102_ready(const vs_max30102_t *dev)
{
    return dev != NULL && dev->ready;
}

uint32_t vs_max30102_read(vs_max30102_t *dev, vs_ppg_sample_t *out, uint32_t max)
{
    if (!vs_max30102_ready(dev) || out == NULL || max == 0) {
        return 0;
    }

    uint8_t wr = 0;
    uint8_t rd = 0;
    uint8_t ovf = 0;
    if (!read_reg(dev, REG_FIFO_WR_PTR, &wr, 1)) return 0;
    if (!read_reg(dev, REG_OVF_COUNTER, &ovf, 1)) return 0;
    if (!read_reg(dev, REG_FIFO_RD_PTR, &rd, 1)) return 0;

    dev->overflow_total += ovf;

    /* The pointers wrap at the FIFO depth, so a write pointer *behind* the read pointer
     * is normal and means the buffer wrapped - not that there is nothing to read. */
    uint32_t available = (uint32_t)((wr + VS_MAX30102_FIFO_DEPTH - rd) % VS_MAX30102_FIFO_DEPTH);
    if (available == 0 && ovf > 0) {
        /* Equal pointers with a non-zero overflow count means exactly full, not empty. */
        available = VS_MAX30102_FIFO_DEPTH;
    }
    if (available > max) {
        available = max;
    }
    if (available == 0) {
        return 0;
    }

    uint32_t produced = 0;
    for (uint32_t i = 0; i < available; ++i) {
        uint8_t raw[BYTES_PER_SAMPLE];
        if (!read_reg(dev, REG_FIFO_DATA, raw, BYTES_PER_SAMPLE)) {
            break; /* keep what was already decoded; the caller sees a short read */
        }
        out[produced].red = vs_max30102_decode(&raw[0]);
        out[produced].ir = vs_max30102_decode(&raw[3]);
        produced++;
    }
    return produced;
}
