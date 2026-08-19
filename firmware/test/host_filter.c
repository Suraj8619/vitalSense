/* Run the firmware's notch over a stream of ADC counts, on the host.
 *
 * Reads whitespace-separated integers on stdin, writes the filtered integers to
 * stdout, one per line. That is the whole program: it exists so that
 * firmware/test/score_notch.py can push real MIT-BIH recordings through the actual
 * device filter and compare the result against the Python design it was generated
 * from (D-25).
 *
 * Usage:  host_filter [--no-prime]
 *   --no-prime  start from zero state instead of priming to the first sample, to show
 *               what the start-up transient would look like without it.
 */
#include <stdio.h>
#include <string.h>

#include "vs_notch.h"

int main(int argc, char **argv)
{
    int prime = 1;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--no-prime") == 0) {
            prime = 0;
        } else {
            fprintf(stderr, "unknown option: %s\n", argv[i]);
            return 2;
        }
    }

    vs_notch_t f;
    vs_notch_reset(&f);

    long value;
    int first = 1;
    while (scanf("%ld", &value) == 1) {
        if (first) {
            if (prime) {
                vs_notch_prime(&f, (int32_t)value);
            } else {
                f.primed = true; /* keep the zero state deliberately */
            }
            first = 0;
        }
        printf("%d\n", vs_notch_process(&f, (int32_t)value));
    }
    return 0;
}
