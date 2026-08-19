/* Leads-off detection: is the patient actually connected?
 *
 * Two independent sources, because either one alone has a blind spot:
 *
 *   1. The AD8232's LO+ / LO- pins, which detect electrode contact electrically by
 *      pushing a small current through the lead and watching whether it returns.
 *   2. The signal itself sitting against a rail. A detached electrode leaves the
 *      amplifier input floating and its output railed, so a saturated ADC reading is
 *      corroborating evidence. This also covers a lead that is still electrically
 *      connected but has come off the skin, which the LO pins can miss.
 *
 * The debounce is deliberately asymmetric (D-28). Asserting is fast because a detached
 * electrode is a safety condition and SR-03 gives the whole system 2 s; clearing is
 * slow because a flapping electrode must not produce an alternating status, and
 * because the amplifier needs time to settle after reconnection anyway - the transient
 * that fabricated a 190 bpm reading in session 006 lives exactly here.
 *
 * Budget note: this debounce composes with the server's 1.5 s alarm debounce inside
 * SR-03's 2 s, which is why it is 50 ms and not 500 ms. A device-side debounce sized
 * without knowing the server's would make the requirement unmeetable the same way the
 * 2 s server debounce did (D-18).
 */
#ifndef VS_LEADSOFF_H
#define VS_LEADSOFF_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VS_LEADSOFF_ASSERT_MS   50.0f
#define VS_LEADSOFF_DEASSERT_MS 500.0f

typedef struct {
    uint32_t assert_samples;
    uint32_t deassert_samples;
    uint32_t run;   /* consecutive samples agreeing with the pending state */
    bool state;     /* debounced output: true = electrodes off */
    bool pending;   /* what the raw evidence currently says */
} vs_leadsoff_t;

/* Initialise for a sampling rate. Starts in the leads-off state: a monitor that has
 * not yet seen evidence of connection must not claim the patient is connected. */
void vs_leadsoff_init(vs_leadsoff_t *d, float fs);

/* Feed one sample. `lo_pins` is LO+ || LO- from the AFE; `counts` is the raw ADC
 * reading. Returns the debounced leads-off state. */
bool vs_leadsoff_update(vs_leadsoff_t *d, bool lo_pins, int32_t counts);

/* True when this ADC reading is against a rail, i.e. the amplifier has clipped. */
bool vs_leadsoff_railed(int32_t counts);

#ifdef __cplusplus
}
#endif

#endif /* VS_LEADSOFF_H */
