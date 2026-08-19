#include "vs_notch.h"

#include "notch_coeffs.h"

#define FRAC VS_NOTCH_FRAC_BITS               /* coefficient fractional bits (Q14) */
#define SFRAC VS_NOTCH_SAMPLE_FRAC_BITS       /* signal fractional bits            */
#define HALF ((int64_t)1 << (FRAC - 1))

/* Arithmetic shift with round-half-away-from-zero. Plain `>> FRAC` rounds toward
 * negative infinity, which is a constant negative bias of half an LSB on every sample.
 * In a recursive filter that bias is fed back and integrates into a slow DC drift, so
 * it is not the harmless truncation it looks like. */
static inline int64_t rshift_round(int64_t v)
{
    return (v >= 0) ? ((v + HALF) >> FRAC) : -(((-v) + HALF) >> FRAC);
}

void vs_notch_reset(vs_notch_t *f)
{
    f->s1 = 0;
    f->s2 = 0;
    f->primed = false;
}

void vs_notch_prime(vs_notch_t *f, int32_t counts)
{
    /* Steady state for a constant input x: y = H(1) * x, then the state follows from
     * the difference equation with every sample equal. For this notch H(1) == 1 by
     * construction - a mains notch must pass DC - but the general form is kept so the
     * priming stays correct if the design changes. */
    const int64_t x = (int64_t)counts << SFRAC;

    const int64_t num = (int64_t)NOTCH_B_Q[0] + NOTCH_B_Q[1] + NOTCH_B_Q[2];
    const int64_t den = (int64_t)NOTCH_A_Q[0] + NOTCH_A_Q[1] + NOTCH_A_Q[2];
    const int64_t y = (den != 0) ? (x * num) / den : x;

    f->s2 = (int64_t)NOTCH_B_Q[2] * x - (int64_t)NOTCH_A_Q[2] * y;
    f->s1 = (int64_t)NOTCH_B_Q[1] * x - (int64_t)NOTCH_A_Q[1] * y + f->s2;
    f->primed = true;
}

int32_t vs_notch_process(vs_notch_t *f, int32_t counts)
{
    if (!f->primed) {
        vs_notch_prime(f, counts);
    }

    const int64_t x = (int64_t)counts << SFRAC;

    const int64_t acc = (int64_t)NOTCH_B_Q[0] * x + f->s1;
    const int64_t y = rshift_round(acc); /* Q(SFRAC) */

    f->s1 = (int64_t)NOTCH_B_Q[1] * x - (int64_t)NOTCH_A_Q[1] * y + f->s2;
    f->s2 = (int64_t)NOTCH_B_Q[2] * x - (int64_t)NOTCH_A_Q[2] * y;

    /* Back to whole counts, rounding rather than truncating for the same reason. */
    const int64_t half = (int64_t)1 << (SFRAC - 1);
    return (int32_t)((y >= 0) ? ((y + half) >> SFRAC) : -(((-y) + half) >> SFRAC));
}

void vs_notch_process_block(vs_notch_t *f, int32_t *counts, uint32_t n)
{
    for (uint32_t i = 0; i < n; ++i) {
        counts[i] = vs_notch_process(f, counts[i]);
    }
}
