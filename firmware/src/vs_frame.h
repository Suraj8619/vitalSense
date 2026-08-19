/* Protocol-v1 frame construction, on the device side.
 *
 * Builds the JSON the server's validateSampleFrame() expects, into a caller-supplied
 * buffer. No malloc: an allocation failure on an ESP32 during a WiFi outage - exactly
 * when the buffer is fullest - is a failure at the worst possible moment.
 *
 * No floating-point formatting either. ADC counts are converted to millivolts in
 * integer arithmetic using the generated constants in vs_calibration.h, then rendered
 * with a hand-written fixed-point formatter. Linking float printf into an ESP32 image
 * costs flash and, on a path adjacent to a 360 Hz timer, time. It is also one more
 * thing whose rounding could differ between the host build and the target build, which
 * would undermine the point of having a host build at all (D-25).
 *
 * The contract this implements is docs/protocol.md. The host test suite feeds the
 * output of this module straight into the real server's validator, so "the firmware
 * speaks the protocol" is checked rather than assumed.
 */
#ifndef VS_FRAME_H
#define VS_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Sensor readings that may legitimately be absent. `null` is sent rather than a stale
 * value: repeating the last good reading makes a dead sensor look healthy, which is
 * the most dangerous failure a monitor has (D-16). */
typedef struct {
    bool spo2_valid;
    int32_t spo2_milli;   /* percent x 1000 */
    bool temp_valid;
    int32_t temp_milli;   /* degrees C x 1000 */
    bool battery_valid;
    int32_t battery_milli; /* volts x 1000 */
} vs_optional_t;

/** Raw photoplethysmogram for one frame. NULL when the sensor has nothing to give. */
typedef struct {
    const uint32_t *red;
    const uint32_t *ir;
    uint32_t n_samples;
    float fs;
} vs_ppg_block_t;

typedef struct {
    const char *device_id;
    uint32_t seq;
    uint32_t t_device_ms;
    float fs;
    const int32_t *ecg_counts;
    uint32_t n_samples;
    bool leads_off;
    vs_optional_t sensors;
    /* Protocol v2. When present the server derives SpO2 from this; when NULL the frame
     * carries `"ppg":null` and the server falls back to `sensors.spo2` if set (D-39). */
    const vs_ppg_block_t *ppg;
} vs_frame_input_t;

/* Convert one ADC count to nanovolts at the electrodes. */
int32_t vs_counts_to_nv(int32_t counts);

/* Render `value_nv` nanovolts as millivolts with 4 decimal places into `dst`.
 * Returns the number of characters written, or 0 if the buffer is too small. */
size_t vs_format_mv(char *dst, size_t cap, int32_t value_nv);

/* Build a complete frame. Returns the number of bytes written (excluding the
 * terminating NUL), or 0 if the buffer was too small - never a truncated frame. A
 * half-written medical data frame is worse than no frame. */
size_t vs_frame_build(char *dst, size_t cap, const vs_frame_input_t *in);

/* Worst-case bytes needed for a frame carrying `n` samples, including the NUL. */
size_t vs_frame_capacity(uint32_t n_samples, size_t device_id_len);

/* As above, with room for `n_ppg` red/infrared sample pairs. */
size_t vs_frame_capacity_with_ppg(uint32_t n_samples, size_t device_id_len, uint32_t n_ppg);

#ifdef __cplusplus
}
#endif

#endif /* VS_FRAME_H */
