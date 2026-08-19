#include "vs_frame.h"

#include <stdio.h>
#include <string.h>

#include "vs_calibration.h"

/* Longest a single sample can render: "-12.3456," */
#define MAX_SAMPLE_CHARS 12
/* Fixed overhead of the envelope, generously rounded up. */
#define ENVELOPE_CHARS 260
/* Widest a raw 18-bit PPG count renders, plus its separator: "262143," */
#define MAX_PPG_CHARS 8

int32_t vs_counts_to_nv(int32_t counts)
{
    return (counts - VS_ADC_ZERO_CODE) * VS_NV_PER_COUNT;
}

size_t vs_format_mv(char *dst, size_t cap, int32_t value_nv)
{
    /* nV -> mV is a division by 1,000,000. Four decimals of a millivolt is 100 nV,
     * which is well below one ADC count (2441 nV), so nothing is lost here. */
    const bool negative = value_nv < 0;
    /* Negate in a wider type: -INT32_MIN does not fit in int32_t. */
    int64_t magnitude = negative ? -(int64_t)value_nv : (int64_t)value_nv;

    /* Round to the 4th decimal place (units of 100 nV) before splitting. */
    magnitude = (magnitude + 50) / 100; /* now in units of 100 nV */
    const int64_t whole = magnitude / 10000;
    const int64_t frac = magnitude % 10000;

    const int n = snprintf(dst, cap, "%s%lld.%04lld", negative && (whole || frac) ? "-" : "",
                           (long long)whole, (long long)frac);
    if (n < 0 || (size_t)n >= cap) {
        return 0;
    }
    return (size_t)n;
}

static size_t format_milli(char *dst, size_t cap, int32_t milli)
{
    const bool negative = milli < 0;
    int64_t magnitude = negative ? -(int64_t)milli : (int64_t)milli;
    const int n = snprintf(dst, cap, "%s%lld.%03lld", negative ? "-" : "",
                           (long long)(magnitude / 1000), (long long)(magnitude % 1000));
    if (n < 0 || (size_t)n >= cap) {
        return 0;
    }
    return (size_t)n;
}

size_t vs_frame_capacity(uint32_t n_samples, size_t device_id_len)
{
    return vs_frame_capacity_with_ppg(n_samples, device_id_len, 0);
}

size_t vs_frame_capacity_with_ppg(uint32_t n_samples, size_t device_id_len, uint32_t n_ppg)
{
    return ENVELOPE_CHARS + device_id_len + (size_t)n_samples * MAX_SAMPLE_CHARS +
           (size_t)n_ppg * 2u * MAX_PPG_CHARS + 1;
}

/* Append to a bounded buffer, tracking overflow in one place rather than at every
 * call site. Once `*used` exceeds `cap` the frame is abandoned. */
static void append(char *dst, size_t cap, size_t *used, const char *src, size_t len)
{
    if (*used + len < cap) {
        memcpy(dst + *used, src, len);
    }
    *used += len;
}

static void append_str(char *dst, size_t cap, size_t *used, const char *src)
{
    append(dst, cap, used, src, strlen(src));
}

size_t vs_frame_build(char *dst, size_t cap, const vs_frame_input_t *in)
{
    if (dst == NULL || in == NULL || in->device_id == NULL || in->ecg_counts == NULL ||
        in->n_samples == 0 || cap == 0) {
        return 0;
    }

    char scratch[64];
    size_t used = 0;

    /* fs is rendered as an integer when it is one, so the server's exact-match check
     * against its configured rate cannot be defeated by a formatting difference. */
    const int fs_whole = (int)(in->fs + 0.5f);
    const int n = snprintf(scratch, sizeof scratch, "{\"v\":2,\"deviceId\":\"");
    append(dst, cap, &used, scratch, (size_t)n);
    append_str(dst, cap, &used, in->device_id);
    const int n2 = snprintf(scratch, sizeof scratch, "\",\"seq\":%lu,\"tDevice\":%lu,\"fs\":%d,\"ecg\":[",
                            (unsigned long)in->seq, (unsigned long)in->t_device_ms, fs_whole);
    append(dst, cap, &used, scratch, (size_t)n2);

    for (uint32_t i = 0; i < in->n_samples; ++i) {
        if (i > 0) {
            append(dst, cap, &used, ",", 1);
        }
        const size_t len = vs_format_mv(scratch, sizeof scratch, vs_counts_to_nv(in->ecg_counts[i]));
        if (len == 0) {
            return 0;
        }
        append(dst, cap, &used, scratch, len);
    }

    append_str(dst, cap, &used, "],\"ppg\":");
    if (in->ppg != NULL && in->ppg->n_samples > 0 && in->ppg->red != NULL && in->ppg->ir != NULL) {
        const int nf = snprintf(scratch, sizeof scratch, "{\"fs\":%d,\"red\":[", (int)(in->ppg->fs + 0.5f));
        append(dst, cap, &used, scratch, (size_t)nf);
        for (uint32_t i = 0; i < in->ppg->n_samples; ++i) {
            if (i > 0) append(dst, cap, &used, ",", 1);
            const int nv = snprintf(scratch, sizeof scratch, "%lu", (unsigned long)in->ppg->red[i]);
            append(dst, cap, &used, scratch, (size_t)nv);
        }
        append_str(dst, cap, &used, "],\"ir\":[");
        for (uint32_t i = 0; i < in->ppg->n_samples; ++i) {
            if (i > 0) append(dst, cap, &used, ",", 1);
            const int nv = snprintf(scratch, sizeof scratch, "%lu", (unsigned long)in->ppg->ir[i]);
            append(dst, cap, &used, scratch, (size_t)nv);
        }
        append_str(dst, cap, &used, "]}");
    } else {
        /* No PPG this frame. Explicitly null, never omitted and never the previous
         * frame's samples - a stale reading is the failure D-16 exists to prevent. */
        append_str(dst, cap, &used, "null");
    }

    append_str(dst, cap, &used, ",\"leadsOff\":");
    append_str(dst, cap, &used, in->leads_off ? "true" : "false");

    /* Absent readings are explicitly null, never omitted and never stale (D-16). */
    append_str(dst, cap, &used, ",\"spo2\":");
    if (in->sensors.spo2_valid) {
        append(dst, cap, &used, scratch, format_milli(scratch, sizeof scratch, in->sensors.spo2_milli));
    } else {
        append_str(dst, cap, &used, "null");
    }

    append_str(dst, cap, &used, ",\"temp\":");
    if (in->sensors.temp_valid) {
        append(dst, cap, &used, scratch, format_milli(scratch, sizeof scratch, in->sensors.temp_milli));
    } else {
        append_str(dst, cap, &used, "null");
    }

    append_str(dst, cap, &used, ",\"battery\":");
    if (in->sensors.battery_valid) {
        append(dst, cap, &used, scratch, format_milli(scratch, sizeof scratch, in->sensors.battery_milli));
    } else {
        append_str(dst, cap, &used, "null");
    }

    append_str(dst, cap, &used, "}");

    if (used + 1 > cap) {
        return 0; /* would have truncated - refuse rather than emit a partial frame */
    }
    dst[used] = '\0';
    return used;
}
