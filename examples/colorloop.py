#!/usr/bin/env python3

import asyncio
from functools import partial

import aiolifx
import aiolifx_effects

class Bulbs:
    def __init__(self):
        self.bulbs = {}

    def register(self, bulb):
        self.bulbs[bulb.mac_addr] = bulb

    def unregister(self, bulb):
        del self.bulbs[bulb.mac_addr]

loop = asyncio.get_event_loop()

conductor = aiolifx_effects.Conductor(loop=loop)

mybulbs = Bulbs()

discover = loop.create_datagram_endpoint(
    partial(aiolifx.LifxDiscovery, loop, mybulbs),
    local_addr=('0.0.0.0', 56700))

loop.create_task(discover)

# Probe
sleep = loop.create_task(asyncio.sleep(1))
loop.run_until_complete(sleep)

# Run effect
devices = list(mybulbs.bulbs.values())
effect = aiolifx_effects.EffectColorloop(period=1)
loop.create_task(conductor.start(effect, devices))

# Stop effect in a while
stop = conductor.stop(devices)
loop.call_later(10, lambda: loop.create_task(stop))

# Wait for completion
sleep = loop.create_task(asyncio.sleep(12))
loop.run_until_complete(sleep)
