/* Sample ring buffer for store-and-forward across a network outage (SR-05).
 *
 * Portable C99, no allocation: the caller supplies the storage, so the buffer's memory
 * cost is visible at the call site rather than hidden in a malloc the ESP32 might not
 * be able to satisfy at 3 a.m. on a ward.
 *
 * Overflow policy is overwrite-oldest, and it is a clinical choice rather than a
 * convenience: if the link has been down longer than the buffer holds, the samples
 * worth keeping are the recent ones. Losing the newest data to preserve a stale
 * backlog would be the wrong trade for a monitor. Overwrites are counted, not
 * silent - `dropped` is what makes the loss reportable instead of invisible (D-27).
 */
#ifndef VS_RINGBUF_H
#define VS_RINGBUF_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int16_t *buf;
    uint32_t capacity;
    uint32_t head;    /* next write position */
    uint32_t count;   /* samples currently held */
    uint32_t dropped; /* samples overwritten before they could be sent */
} vs_ringbuf_t;

/* `storage` must hold at least `capacity` int16_t and outlive the buffer. */
bool vs_ringbuf_init(vs_ringbuf_t *rb, int16_t *storage, uint32_t capacity);

void vs_ringbuf_reset(vs_ringbuf_t *rb);

/* Append one sample, overwriting the oldest if full. */
void vs_ringbuf_push(vs_ringbuf_t *rb, int16_t v);

/* Append `n` samples. Returns the number of old samples overwritten. */
uint32_t vs_ringbuf_push_block(vs_ringbuf_t *rb, const int16_t *src, uint32_t n);

/* Remove up to `n` of the oldest samples into `dst`. Returns how many were taken. */
uint32_t vs_ringbuf_pop_block(vs_ringbuf_t *rb, int16_t *dst, uint32_t n);

/* Read the oldest samples without removing them - a frame that has been handed to the
 * network stack is not gone until the send succeeds. */
uint32_t vs_ringbuf_peek_block(const vs_ringbuf_t *rb, int16_t *dst, uint32_t n);

/* Discard the oldest `n` samples, after a peeked frame has been acknowledged. */
uint32_t vs_ringbuf_discard(vs_ringbuf_t *rb, uint32_t n);

static inline uint32_t vs_ringbuf_count(const vs_ringbuf_t *rb) { return rb->count; }
static inline uint32_t vs_ringbuf_free(const vs_ringbuf_t *rb) { return rb->capacity - rb->count; }
static inline bool vs_ringbuf_full(const vs_ringbuf_t *rb) { return rb->count == rb->capacity; }

#ifdef __cplusplus
}
#endif

#endif /* VS_RINGBUF_H */
