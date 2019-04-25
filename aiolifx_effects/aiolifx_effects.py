import asyncio
import random

from functools import partial

# aiolifx waveform modes
WAVEFORM_SINE = 1
WAVEFORM_PULSE = 4

NEUTRAL_WHITE = 3500

def lifx_white(device):
    return device.product and device.product in [10, 11, 18]

class PreState:
    """Structure describing a power/color state."""

    def __init__(self, device):
        self.power = (device.power_level != 0)
        self.color = list(device.color)
        if device.color_zones:
            self.color_zones = device.color_zones.copy()
        else:
            self.color_zones = None


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

    async def wait(self, method):
        """Call an aiolifx method and wait for its response or a timeout."""
        self.event.clear()
        method(callb=self.callback)
        await self.event.wait()
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

    async def start(self, effect, participants):
        if not participants:
            return

        async with self.lock:
            effect.conductor = self

            # Restore previous state
            await self._stop_nolock(participants, effect)

            # Remember the current state
            tasks = []
            for device in participants:
                if not self.running.get(device.mac_addr):
                    tasks.append(AwaitAioLIFX().wait(device.get_color))
                    if device.color_zones:
                        for zone in range(0, len(device.color_zones), 8):
                            tasks.append(AwaitAioLIFX().wait(partial(device.get_color_zones, start_index=zone)))
            if tasks:
                await asyncio.wait(tasks)

            for device in participants:
                running = self.running.get(device.mac_addr)
                pre_state = running.pre_state if running else PreState(device)
                self.running[device.mac_addr] = RunningEffect(effect, pre_state)

            # Powered off zones report zero brightness. Get the real values.
            await self._fixup_multizone(participants)

            self.loop.create_task(effect.async_perform(participants))

    async def stop(self, devices):
        async with self.lock:
            await self._stop_nolock(devices)

    async def _stop_nolock(self, devices, new_effect=None):
        tasks = []
        for device in devices:
            tasks.append(self.loop.create_task(self._stop_one(device, new_effect)))
        if tasks:
            await asyncio.wait(tasks)

    async def _stop_one(self, device, new_effect):
        running = self.running.get(device.mac_addr)
        if not running:
            return
        effect = running.effect

        index = next(i for i,p in enumerate(effect.participants) if p.mac_addr == device.mac_addr)
        effect.participants.pop(index)

        if new_effect and effect.inherit_prestate(new_effect):
            return

        del self.running[device.mac_addr]

        if not running.pre_state.power:
            device.set_power(False)
            await asyncio.sleep(0.3)

        ack = AwaitAioLIFX().wait

        zones = running.pre_state.color_zones
        if zones:
            for index, zone_hsbk in enumerate(zones):
                apply = 1 if (index == len(zones)-1) else 0
                await ack(partial(device.set_color_zones,
                    index, index, zone_hsbk, apply=apply))
        else:
            await ack(partial(device.set_color,
                running.pre_state.color))

        await asyncio.sleep(0.3)

    async def _fixup_multizone(self, participants):
        """Temporarily turn on multizone lights to get the correct zone states."""
        fixup = []
        for device in participants:
            if device.color_zones and device.power_level == 0:
                fixup.append(device)

        if not fixup:
            return

        async def powertoggle(state):
            tasks = []
            for device in fixup:
                tasks.append(AwaitAioLIFX().wait(partial(device.set_power, state)))
            await asyncio.wait(tasks)
            await asyncio.sleep(0.3)

        # Power on
        await powertoggle(True)

        # Get full hsbk
        tasks = []
        for device in fixup:
            for zone in range(0, len(device.color_zones), 8):
                tasks.append(AwaitAioLIFX().wait(partial(device.get_color_zones, start_index=zone)))
        await asyncio.wait(tasks)

        # Update pre_state colors
        for device in fixup:
            for zone in range(0, len(device.color_zones)):
                self.running[device.mac_addr].pre_state.color_zones[zone] = device.color_zones[zone]

        # Power off again
        await powertoggle(False)


class LIFXEffect:
    """Representation of a light effect running on a number of lights."""

    def __init__(self, power_on=True):
        """Initialize the effect."""
        self.power_on = power_on
        self.conductor = None
        self.participants = None

    async def async_perform(self, participants):
        """Do common setup and play the effect."""
        self.participants = participants

        # Temporarily turn on power for the effect to be visible
        tasks = []
        for device in self.participants:
            if self.power_on and not device.power_level:
                tasks.append(self.conductor.loop.create_task(self.poweron(device)))
        if tasks:
            await asyncio.wait(tasks)

        await self.async_play()

    async def poweron(self, device):
        hsbk = await self.from_poweroff_hsbk(device)
        device.set_color(hsbk)
        device.set_power(True)
        await asyncio.sleep(0.1)

    async def async_play(self):
        """Play the effect."""
        return None

    async def from_poweroff_hsbk(self, device):
        """Return the color when starting from a powered off state."""
        return [random.randint(0, 65535), 65535, 0, NEUTRAL_WHITE]

    def running(self, device):
        return self.conductor.running[device.mac_addr]

    def inherit_prestate(self, other):
        """Returns True if two effects can run without a reset."""
        return False


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
            self.skew_ratio = 2**15 - ping_duration
        elif self.mode == 'solid':
            self.skew_ratio = -2**15
        else:
            self.skew_ratio = 0

    async def async_play(self):
        """Play the effect on all lights."""
        for device in self.participants:
            self.conductor.loop.create_task(self.async_light_play(device))

        # Wait for completion and restore the initial state on remaining participants
        await asyncio.sleep(self.period*self.cycles)
        await self.conductor.stop(self.participants)

    async def async_light_play(self, device):
        """Play a light effect on the bulb."""

        # Strobe must flash from a dark color
        if self.mode == 'strobe':
            device.set_color([0, 0, 0, NEUTRAL_WHITE])
            await asyncio.sleep(0.1)

        # Now run the effect
        color = await self.effect_color(device)
        args = {
            'transient': 1,
            'color': color,
            'period': int(self.period*1000),
            'cycles': self.cycles,
            'skew_ratio': self.skew_ratio,
            'duty_cycle': self.skew_ratio,
            'waveform': self.waveform,
        }
        device.set_waveform(args)

    async def from_poweroff_hsbk(self, device):
        """Start with the target color, but no brightness."""
        to_hsbk = await self.effect_color(device)
        return [to_hsbk[0], to_hsbk[1], 0, to_hsbk[2]]

    async def effect_color(self, device):
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

    def inherit_prestate(self, other):
        """Returns True if two effects can run without a reset."""
        return type(self) == type(other)

    async def async_play(self, **kwargs):
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

            await asyncio.sleep(self.period)
