#include "vs_ringbuf.h"

#include <string.h>

static inline uint32_t tail_index(const vs_ringbuf_t *rb)
{
    /* Oldest sample: head minus count, modulo capacity. */
    return (rb->head + rb->capacity - rb->count) % rb->capacity;
}

bool vs_ringbuf_init(vs_ringbuf_t *rb, int16_t *storage, uint32_t capacity)
{
    if (rb == NULL || storage == NULL || capacity == 0) {
        return false;
    }
    rb->buf = storage;
    rb->capacity = capacity;
    vs_ringbuf_reset(rb);
    return true;
}

void vs_ringbuf_reset(vs_ringbuf_t *rb)
{
    rb->head = 0;
    rb->count = 0;
    rb->dropped = 0;
}

void vs_ringbuf_push(vs_ringbuf_t *rb, int16_t v)
{
    rb->buf[rb->head] = v;
    rb->head = (rb->head + 1) % rb->capacity;
    if (rb->count < rb->capacity) {
        rb->count++;
    } else {
        /* Full: the write above landed on the oldest sample. */
        rb->dropped++;
    }
}

uint32_t vs_ringbuf_push_block(vs_ringbuf_t *rb, const int16_t *src, uint32_t n)
{
    const uint32_t before = rb->dropped;
    for (uint32_t i = 0; i < n; ++i) {
        vs_ringbuf_push(rb, src[i]);
    }
    return rb->dropped - before;
}

uint32_t vs_ringbuf_peek_block(const vs_ringbuf_t *rb, int16_t *dst, uint32_t n)
{
    const uint32_t take = (n < rb->count) ? n : rb->count;
    uint32_t idx = tail_index(rb);
    for (uint32_t i = 0; i < take; ++i) {
        dst[i] = rb->buf[idx];
        idx = (idx + 1) % rb->capacity;
    }
    return take;
}

uint32_t vs_ringbuf_discard(vs_ringbuf_t *rb, uint32_t n)
{
    const uint32_t drop = (n < rb->count) ? n : rb->count;
    rb->count -= drop;
    return drop;
}

uint32_t vs_ringbuf_pop_block(vs_ringbuf_t *rb, int16_t *dst, uint32_t n)
{
    const uint32_t take = vs_ringbuf_peek_block(rb, dst, n);
    vs_ringbuf_discard(rb, take);
    return take;
}
