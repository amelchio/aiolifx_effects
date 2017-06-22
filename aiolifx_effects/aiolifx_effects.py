import asyncio
import random

from functools import partial

# aiolifx waveform modes
WAVEFORM_SINE = 1
WAVEFORM_PULSE = 4

NEUTRAL_WHITE = 3500

def lifx_white(device):
    return device.product and device.product in [10, 11, 18]

class PowerColor:
    """Structure describing a power/color state."""

    def __init__(self, power, color):
        self.power = power
        self.color = list(color)


class RunningEffect:
    """Structure describing a running effect."""

    def __init__(self, effect, pre_state):
        self.effect = effect
        self.pre_state = pre_state


class AwaitAioLIFX:
    """Wait for an aiolifx callback and return the message."""

    def __init__(self):
        """Initialize the wrapper."""
        self.device = None
        self.message = None
        self.event = asyncio.Event()

    def callback(self, device, message):
        """Handle responses."""
        self.device = device
        self.message = message
        self.event.set()

    @asyncio.coroutine
    def wait(self, method):
        """Call an aiolifx method and wait for its response or a timeout."""
        self.event.clear()
        method(self.callback)
        yield from self.event.wait()
        return self.message


class Conductor:

    def __init__(self, loop):
        self.loop = loop
        self.running = {}
        self.lock = asyncio.Lock()

    def effect(self, device):
        """Return the effect currently running on a device."""
        if device.mac_addr in self.running:
            return self.running[device.mac_addr].effect
        else:
            return None

    @asyncio.coroutine
    def start(self, effect, participants):
        yield from self.lock.acquire()

        effect.conductor = self

        # Restore previous state
        yield from self._stop_nolock(participants)

        # Remember the current state
        tasks = []
        for device in participants:
            tasks.append(AwaitAioLIFX().wait(device.get_color))
        yield from asyncio.wait(tasks, loop=self.loop)

        for device in participants:
            pre_state = PowerColor((device.power_level != 0), device.color)
            self.running[device.mac_addr] = RunningEffect(effect, pre_state)

        self.loop.create_task(effect.async_perform(participants))
        self.lock.release()

    @asyncio.coroutine
    def stop(self, devices):
        yield from self.lock.acquire()
        yield from self._stop_nolock(devices)
        self.lock.release()

    @asyncio.coroutine
    def _stop_nolock(self, devices):
        tasks = []
        for device in devices:
            tasks.append(self.loop.create_task(self._stop_one(device)))
        yield from asyncio.wait(tasks, loop=self.loop)

    @asyncio.coroutine
    def _stop_one(self, device):
        running = self.running.get(device.mac_addr, None)
        if not running:
            return
        effect = running.effect

        del self.running[device.mac_addr]

        index = next(i for i,p in enumerate(effect.participants) if p.mac_addr == device.mac_addr)
        effect.participants.pop(index)

        if not running.pre_state.power:
            device.set_power(False)
            yield from asyncio.sleep(0.3)

        device.set_color(running.pre_state.color)
        yield from asyncio.sleep(0.3)


class LIFXEffect:
    """Representation of a light effect running on a number of lights."""

    def __init__(self, power_on=True):
        """Initialize the effect."""
        self.power_on = power_on
        self.conductor = None
        self.participants = None

    @asyncio.coroutine
    def async_perform(self, participants):
        """Do common setup and play the effect."""
        self.participants = participants

        # Temporarily turn on power for the effect to be visible
        tasks = []
        for device in self.participants:
            if self.power_on and not device.power_level:
                tasks.append(self.conductor.loop.create_task(self.poweron(device)))
        if tasks:
            yield from asyncio.wait(tasks, loop=self.conductor.loop)

        yield from self.async_play()

    @asyncio.coroutine
    def poweron(self, device):
        hsbk = yield from self.from_poweroff_hsbk(device)
        device.set_color(hsbk)
        device.set_power(True)
        yield from asyncio.sleep(0.1)

    @asyncio.coroutine
    def async_play(self):
        """Play the effect."""
        yield None

    @asyncio.coroutine
    def from_poweroff_hsbk(self, device):
        """Return the color when starting from a powered off state."""
        return [random.randint(0, 65535), 65535, 0, NEUTRAL_WHITE]

    def running(self, device):
        return self.conductor.running[device.mac_addr]


class EffectPulse(LIFXEffect):
    """Representation of a pulse effect."""

    def __init__(self, power_on=True, mode=None, period=None, cycles=None, hsbk=None):
        """Initialize the pulse effect."""
        super().__init__(power_on)
        self.name = 'pulse'

        self.mode = mode if mode else 'blink'

        if self.mode == 'strobe':
            default_period = 0.1
            default_cycles = 10
        else:
            default_period = 1.0
            default_cycles = 1

        self.period = period if period else default_period
        self.cycles = cycles if cycles else default_cycles

        self.hsbk = hsbk

        # Breathe has a special waveform
        if self.mode == 'breathe':
            self.waveform = WAVEFORM_SINE
        else:
            self.waveform = WAVEFORM_PULSE

        # Ping and solid have special duty cycles
        if self.mode == 'ping':
            ping_duration = int(5000 - min(2500, 300*self.period))
            self.duty_cycle = 2**15 - ping_duration
        elif self.mode == 'solid':
            self.duty_cycle = -2**15
        else:
            self.duty_cycle = 0

    @asyncio.coroutine
    def async_play(self):
        """Play the effect on all lights."""
        for device in self.participants:
            self.conductor.loop.create_task(self.async_light_play(device))

        # Wait for completion and restore the initial state on remaining participants
        yield from asyncio.sleep(self.period*self.cycles)
        if self.participants:
            yield from self.conductor.stop(self.participants)

    @asyncio.coroutine
    def async_light_play(self, device):
        """Play a light effect on the bulb."""

        # Strobe must flash from a dark color
        if self.mode == 'strobe':
            device.set_color([0, 0, 0, NEUTRAL_WHITE])
            yield from asyncio.sleep(0.1)

        # Now run the effect
        color = yield from self.effect_color(device)
        args = {
            'transient': 1,
            'color': color,
            'period': int(self.period*1000),
            'cycles': self.cycles,
            'duty_cycle': self.duty_cycle,
            'waveform': self.waveform,
        }
        device.set_waveform(args)

    @asyncio.coroutine
    def from_poweroff_hsbk(self, device):
        """Start with the target color, but no brightness."""
        to_hsbk = yield from self.effect_color(device)
        return [to_hsbk[0], to_hsbk[1], 0, to_hsbk[2]]

    @asyncio.coroutine
    def effect_color(self, device):
        pre_state = self.running(device).pre_state
        base = list(pre_state.color)

        if self.hsbk:
            # Use the values provided in hsbk (but skip parts with None)
            return list(map(lambda x,y: y if y is not None else x, base, self.hsbk))
        else:
            # Set default effect color based on current setting
            hsbk = base
            if self.mode == 'strobe':
                # Strobe: cold white
                hsbk = [hsbk[0], 0, 65535, 5600]
            elif lifx_white(device) or hsbk[1] < 65536/2:
                # White: toggle brightness
                hsbk[2] = 0 if (hsbk[2] > 65536/2 and pre_state.power) else 65535
            else:
                # Color: fully desaturate with full brightness
                hsbk = [hsbk[0], 0, 65535, 4000]
            return hsbk


class EffectColorloop(LIFXEffect):
    """Representation of a colorloop effect."""

    def __init__(self, power_on=True, period=None, change=None, spread=None, brightness=None, transition=None):
        """Initialize the colorloop effect."""
        super().__init__(power_on)
        self.name = 'colorloop'

        self.period = period if period else 60
        self.change = change if change else 20
        self.spread = spread if spread else 30
        self.brightness = brightness
        self.transition = transition

    @asyncio.coroutine
    def async_play(self, **kwargs):
        """Play the effect on all lights."""
        # Random start
        hue = random.uniform(0, 360) % 360
        direction = 1 if random.randint(0, 1) else -1

        while self.participants:
            hue = (hue + direction*self.change) % 360
            lhue = hue

            random.shuffle(self.participants)

            for device in self.participants:
                if self.transition is not None:
                    transition = int(1000*self.transition)
                elif device == self.participants[0] or self.spread > 0:
                    transition = int(1000 * random.uniform(self.period/2, self.period))

                if self.brightness is not None:
                    brightness = self.brightness
                else:
                    brightness = self.running(device).pre_state.color[2]

                hsbk = [
                    int(65535/360*lhue),
                    int(random.uniform(0.8, 1.0)*65535),
                    brightness,
                    NEUTRAL_WHITE,
                ]
                device.set_color(hsbk, None, transition)

                # Adjust the next light so the full spread is used
                if len(self.participants) > 1:
                    lhue = (lhue + self.spread/(len(self.participants)-1)) % 360

            yield from asyncio.sleep(self.period)
