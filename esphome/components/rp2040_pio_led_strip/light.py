from dataclasses import dataclass

import os

from esphome import pins
from esphome.components import light, rp2040
from esphome.const import (
    CONF_CHIPSET,
    CONF_NUM_LEDS,
    CONF_OUTPUT_ID,
    CONF_PIN,
    CONF_RGB_ORDER,
)

import esphome.codegen as cg
import esphome.config_validation as cv

from esphome.loader import CORE

import esphome.platformio_api as api

from esphome.util import _LOGGER


def get_nops(timing):
    """
    Calculate the number of NOP instructions required to wait for a given amount of time.
    """
    time_remaining = timing
    nops = []
    if time_remaining < 32:
        nops.append(time_remaining - 1)
        return nops
    nops.append(31)
    time_remaining -= 32
    while time_remaining > 0:
        if time_remaining >= 32:
            nops.append("nop [31]")
            time_remaining -= 32
        else:
            nops.append("nop [" + str(time_remaining) + " - 1 ]")
            time_remaining = 0
    return nops


def generate_assembly_code(pio, rgbw, t0h, t0l, t1h, t1l):
    """
    Generate assembly code with the given timing values.
    """
    nops_t0h = get_nops(t0h)
    nops_t0l = get_nops(t0l)
    nops_t1h = get_nops(t1h)
    nops_t1l = get_nops(t1l)

    t0h = nops_t0h.pop(0)
    t0l = nops_t0l.pop(0)
    t1h = nops_t1h.pop(0)
    t1l = nops_t1l.pop(0)

    nops_t0h = "\n".join(" " * 4 + nop for nop in nops_t0h)
    nops_t0l = "\n".join(" " * 4 + nop for nop in nops_t0l)
    nops_t1h = "\n".join(" " * 4 + nop for nop in nops_t1h)
    nops_t1l = "\n".join(" " * 4 + nop for nop in nops_t1l)

    const_csdk_code = (
        """
% c-sdk {
#include "hardware/clocks.h"
"""
        + """
static inline void rp2040_pio_driver{}_init(PIO pio, uint sm, uint offset, uint pin, float freq)""".format(
            pio
        )
        + """ {
    pio_gpio_init(pio, pin);
    pio_sm_set_consecutive_pindirs(pio, sm, pin, 1, true);
"""
        + """
    pio_sm_config c = rp2040_pio_led_driver{}_program_get_default_config(offset);""".format(
            pio
        )
        + """
    sm_config_set_set_pins(&c, pin, 1);
    sm_config_set_out_shift(&c, false, true, 24);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);

    int cycles_per_bit = 69;
    float div = 2.409;
    sm_config_set_clkdiv(&c, div);


    pio_sm_init(pio, sm, offset, &c);
    pio_sm_set_enabled(pio, sm, true);
}
%}"""
    )

    assembly_template = """.program rp2040_pio_led_driver{}

.wrap_target
awaiting_data:
    ; Wait for data in FIFO queue
    pull block ; this will block until there is data in the FIFO queue and then it will pull it into the shift register
    set y, {} ; set y to the number of bits to write, (24 if RGB, 32 if RGBW)

mainloop:
    ; go through each bit in the shift register and jump to the appropriate label
    ; depending on the value of the bit

    out x, 1
    jmp !x, writezero
    jmp writeone

writezero:
    ; Write T0H and T0L bits to the output pin
    set pins, 1 [{}]
{}
    set pins, 0 [{}]
{}
    jmp y--, mainloop
    jmp awaiting_data

writeone:
    ; Write T1H and T1L bits to the output pin
    set pins, 1 [{}]
{}
    set pins, 0 [{}]
{}
    jmp y--, mainloop
    jmp awaiting_data

.wrap""".format(
        pio,
        32 if rgbw else 24,
        t0h,
        nops_t0h,
        t0l,
        nops_t0l,
        t1h,
        nops_t1h,
        t1l,
        nops_t1l,
    )

    return assembly_template + const_csdk_code


def time_to_cycles(time_us):
    cycles_per_us = 57.5
    cycles = round(float(time_us) * cycles_per_us)
    return cycles


CONF_PIO = "pio"

CODEOWNERS = ["@Papa-DMan"]
DEPENDENCIES = ["rp2040"]

rp2040_pio_led_strip_ns = cg.esphome_ns.namespace("rp2040_pio_led_strip")
RP2040PIOLEDStripLightOutput = rp2040_pio_led_strip_ns.class_(
    "RP2040PIOLEDStripLightOutput", light.AddressableLight
)

RGBOrder = rp2040_pio_led_strip_ns.enum("RGBOrder")

Chipsets = rp2040_pio_led_strip_ns.enum("Chipset")


@dataclass
class LEDStripTimings:
    T0H: int
    T0L: int
    T1H: int
    T1L: int


RGB_ORDERS = {
    "RGB": RGBOrder.ORDER_RGB,
    "RBG": RGBOrder.ORDER_RBG,
    "GRB": RGBOrder.ORDER_GRB,
    "GBR": RGBOrder.ORDER_GBR,
    "BGR": RGBOrder.ORDER_BGR,
    "BRG": RGBOrder.ORDER_BRG,
}

CHIPSETS = {
    "WS2812": LEDStripTimings(20, 43, 41, 31),
    "WS2812B": LEDStripTimings(23, 46, 46, 23),
    "SK6812": LEDStripTimings(17, 52, 31, 31),
    "SM16703": LEDStripTimings(17, 52, 52, 17),
}

CONF_IS_RGBW = "is_rgbw"
CONF_T0H = "bit0_high"
CONF_T0L = "bit0_low"
CONF_T1H = "bit1_high"
CONF_T1L = "bit1_low"

PIO_VALUES = {rp2040.const: [0, 1]}


def _validate_pio_value(value):
    value = cv.int_(value)
    if value < 0 or value > 1:
        raise cv.Invalid("Value must be between 0 and 1")
    return value


def _validate_timing(value):
    # if doesn't end with us, raise error
    if not value.endswith("us"):
        raise cv.Invalid("Timing must be in microseconds (us)")
    value = float(value[:-2])
    nops = get_nops(value)
    nops.pop(0)
    if len(nops) > 3:
        raise cv.Invalid("Timing is too long, please try again.")
    return value


CONFIG_SCHEMA = cv.All(
    light.ADDRESSABLE_LIGHT_SCHEMA.extend(
        {
            cv.GenerateID(CONF_OUTPUT_ID): cv.declare_id(RP2040PIOLEDStripLightOutput),
            cv.Required(CONF_PIN): pins.internal_gpio_output_pin_number,
            cv.Required(CONF_NUM_LEDS): cv.positive_not_null_int,
            cv.Required(CONF_RGB_ORDER): cv.enum(RGB_ORDERS, upper=True),
            cv.Required(CONF_PIO): _validate_pio_value,
            cv.Optional(CONF_CHIPSET): cv.one_of(*CHIPSETS, upper=True),
            cv.Optional(CONF_IS_RGBW, default=False): cv.boolean,
            cv.Inclusive(
                CONF_T0H,
                "custom",
            ): _validate_timing,
            cv.Inclusive(
                CONF_T0L,
                "custom",
            ): _validate_timing,
            cv.Inclusive(
                CONF_T1H,
                "custom",
            ): _validate_timing,
            cv.Inclusive(
                CONF_T1L,
                "custom",
            ): _validate_timing,
        }
    ),
    cv.has_exactly_one_key(CONF_CHIPSET, CONF_T0H),
)


async def to_code(config):
    print(CORE.build_path)
    var = cg.new_Pvariable(config[CONF_OUTPUT_ID])
    await light.register_light(var, config)
    await cg.register_component(var, config)

    cg.add(var.set_num_leds(config[CONF_NUM_LEDS]))
    cg.add(var.set_pin(config[CONF_PIN]))

    cg.add(var.set_rgb_order(config[CONF_RGB_ORDER]))
    cg.add(var.set_is_rgbw(config[CONF_IS_RGBW]))

    cg.add(var.set_pio(config[CONF_PIO]))

    # generate both empty headers if they don't exist yet
    if not os.path.isfile(
        CORE.build_path + "/src/esphome/components/rp2040_pio_led_strip/Driver0.pio"
    ):
        with open(
            CORE.build_path
            + "/src/esphome/components/rp2040_pio_led_strip/Driver0.pio.h",
            "w",
        ) as f:
            f.write("//nothing")
    if not os.path.isfile(
        CORE.build_path + "src/esphome/components/rp2040_pio_led_strip/Driver1.pio"
    ):
        with open(
            CORE.build_path
            + "/src/esphome/components/rp2040_pio_led_strip/Driver1.pio.h",
            "w",
        ) as f:
            f.write("//nothing")

    if CONF_IS_RGBW in config:
        is_rgbw = config[CONF_IS_RGBW]
    else:
        is_rgbw = False

    if CONF_CHIPSET in config:
        _LOGGER.debug("Generating PIO assembly code")
        with open(
            CORE.build_path
            + "/src/esphome/components/rp2040_pio_led_strip/Driver{}.pio".format(
                config[CONF_PIO]
            ),
            "w",
        ) as f:
            f.write(
                generate_assembly_code(
                    config[CONF_PIO],
                    is_rgbw,
                    CHIPSETS[config[CONF_CHIPSET]].T0H,
                    CHIPSETS[config[CONF_CHIPSET]].T0L,
                    CHIPSETS[config[CONF_CHIPSET]].T1H,
                    CHIPSETS[config[CONF_CHIPSET]].T1L,
                )
            )
    else:
        _LOGGER.debug("Generating custom PIO assembly code")

        with open(
            CORE.build_path
            + "/src/esphome/components/rp2040_pio_led_strip/Driver{}.pio".format(
                config[CONF_PIO]
            ),
            "w",
        ) as f:
            f.write(
                generate_assembly_code(
                    config[CONF_PIO],
                    is_rgbw,
                    time_to_cycles(config[CONF_T0H]),
                    time_to_cycles(config[CONF_T0L]),
                    time_to_cycles(config[CONF_T1H]),
                    time_to_cycles(config[CONF_T1L]),
                )
            )

    _LOGGER.debug("Assembling PIO assembly code")
    api.run_platformio_cli(
        "pkg",
        "exec",
        "--package",
        "platformio/tool-rp2040tools",
        "--",
        "pioasm",
        CORE.build_path
        + "/src/esphome/components/rp2040_pio_led_strip/Driver{}.pio".format(
            config[CONF_PIO]
        ),
        CORE.build_path
        + "/src/esphome/components/rp2040_pio_led_strip/Driver{}.pio.h".format(
            config[CONF_PIO]
        ),
    )

    with open(
        CORE.build_path
        + "/src/esphome/components/rp2040_pio_led_strip/Driver{}.pio.h".format(
            config[CONF_PIO]
        ),
    ) as f:
        code = f.read()
        code = (
            "#ifndef __DRIVER{}_PIO_H__\n#define __DRIVER{}_PIO_H__\n\n".format(
                config[CONF_PIO], config[CONF_PIO]
            )
            + code
            + "\n#endif\n"
        )
    with open(
        CORE.build_path
        + "/src/esphome/components/rp2040_pio_led_strip/Driver{}.pio.h".format(
            config[CONF_PIO]
        ),
        "w",
    ) as f:
        f.write(code)
