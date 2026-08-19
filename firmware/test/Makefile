# Host build of the portable firmware core.
#
# The point of this Makefile is D-25: the C that runs on the ESP32 is compiled and
# exercised here, on a laptop, against the same MIT-BIH recordings that validate the
# Python and JavaScript implementations. Firmware that can only be run on hardware can
# only be trusted on hardware.

CC      ?= cc
CFLAGS  ?= -std=c99 -Wall -Wextra -Werror -O2
INCLUDE := -I../src
LDLIBS  := -lm
SRC     := ../src/vs_notch.c ../src/vs_ringbuf.c ../src/vs_leadsoff.c ../src/vs_frame.c ../src/vs_max30102.c
BUILD   := build

.PHONY: all test filter device clean

all: $(BUILD)/test_firmware $(BUILD)/host_filter $(BUILD)/host_device

$(BUILD):
	@mkdir -p $(BUILD)

$(BUILD)/test_firmware: test_firmware.c $(SRC) | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDE) $^ $(LDLIBS) -o $@

$(BUILD)/host_filter: host_filter.c $(SRC) | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDE) $^ $(LDLIBS) -o $@

$(BUILD)/host_device: host_device.c $(SRC) | $(BUILD)
	$(CC) $(CFLAGS) $(INCLUDE) $^ $(LDLIBS) -o $@

test: $(BUILD)/test_firmware
	@./$(BUILD)/test_firmware

filter: $(BUILD)/host_filter

device: $(BUILD)/host_device

clean:
	@rm -rf $(BUILD)
