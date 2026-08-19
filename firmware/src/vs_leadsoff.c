#include "vs_leadsoff.h"

#include "vs_calibration.h"

static uint32_t ms_to_samples(float fs, float ms)
{
    const float n = fs * ms / 1000.0f;
    return (n < 1.0f) ? 1u : (uint32_t)(n + 0.5f);
}

bool vs_leadsoff_railed(int32_t counts)
{
    return counts <= VS_RAIL_MARGIN || counts >= (VS_ADC_FULL_SCALE - VS_RAIL_MARGIN);
}

void vs_leadsoff_init(vs_leadsoff_t *d, float fs)
{
    d->assert_samples = ms_to_samples(fs, VS_LEADSOFF_ASSERT_MS);
    d->deassert_samples = ms_to_samples(fs, VS_LEADSOFF_DEASSERT_MS);
    d->run = 0;
    /* Start disconnected. "I do not know yet" and "the patient is connected" are very
     * different claims, and only one of them is safe to make by default. */
    d->state = true;
    d->pending = true;
}

bool vs_leadsoff_update(vs_leadsoff_t *d, bool lo_pins, int32_t counts)
{
    const bool raw = lo_pins || vs_leadsoff_railed(counts);

    if (raw == d->state) {
        /* Evidence agrees with the current state; nothing pending. */
        d->run = 0;
        d->pending = raw;
        return d->state;
    }

    if (raw != d->pending) {
        d->pending = raw;
        d->run = 0;
    }

    d->run++;
    const uint32_t needed = raw ? d->assert_samples : d->deassert_samples;
    if (d->run >= needed) {
        d->state = raw;
        d->run = 0;
    }
    return d->state;
}
