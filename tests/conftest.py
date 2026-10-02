"""Shared fixtures for the DNP3 interface test suite. These run without a platform, a proxy, or an outstation."""
import json

from unittest import mock

import pytest

from gevent.event import AsyncResult

from volttron.driver.base.config import RemoteConfig
from volttron.driver.interfaces.dnp3.dnp3 import Dnp3
from volttron.driver.interfaces.dnp3.config import Dnp3PointConfig


class FakePPM:
    """Stands in for GeventProtocolProxyManager: records payloads, replays queued replies, runs registration hooks."""

    def __init__(self):
        self.sent: list[tuple[str, dict, bool]] = []
        self.replies: list = []
        self.callbacks: dict[str, object] = {}
        self.peer = object()
        self.started = 0
        self.launch: tuple | None = None
        self.registration_waits: list[float] = []

    def queue(self, *replies):
        self.replies.extend(replies)

    def register_callback(self, fn, name, provides_response=False, timeout=30.0):
        self.callbacks[name] = fn

    def start(self):
        self.started += 1

    def select_loop(self):
        pass

    def get_proxy(self, key, **kwargs):
        self.launch = (key, kwargs)
        return self.peer

    def wait_peer_registered(self, peer, timeout, func=None, *args, **kwargs):
        self.registration_waits.append(timeout)
        if func:
            func(*args, **kwargs)

    def send(self, peer, message):
        self.sent.append((message.method_name, json.loads(message.payload.decode('utf8')), message.response_expected))
        reply = self.replies.pop(0) if self.replies else serialized({})
        if isinstance(reply, (bytes, bytearray)):
            result = AsyncResult()
            result.set(reply)
            return result
        return reply       # e.g. False, to simulate an unsendable request

    def payloads(self, method_name=None):
        return [p for m, p, _ in self.sent if method_name is None or m == method_name]


def serialized(result, error=None):
    """A reply as the proxy's serializer would produce it."""
    return json.dumps({'result': result, 'error': error if error is not None else {}}).encode('utf8')


def reading(value, quality=1, online=True):
    return {'value': value, 'quality': quality, 'online': online, 'timestamp': None}


def point(name, group, index, variation=None, writable=False, **extra):
    return Dnp3PointConfig(volttron_point_name=name, group=group, index=index, variation=variation, writable=writable,
                           units=extra.pop('units', ''), **extra)


@pytest.fixture
def ppm():
    return FakePPM()


@pytest.fixture
def driver_agent():
    return mock.Mock()


@pytest.fixture
def make_interface(ppm, driver_agent):
    """Build a real Dnp3 interface whose proxy manager is the FakePPM."""
    def build(points=(), base_topic='campus/building/der', **remote):
        remote = {'driver_type': 'dnp3', 'outstation_ip': '10.0.0.5', **remote}
        Dnp3.default_config = {}
        with mock.patch('volttron.driver.interfaces.dnp3.dnp3.GeventProtocolProxyManager') as manager_class:
            manager_class.get_manager.return_value = ppm
            interface = Dnp3(RemoteConfig(**remote), driver_agent=driver_agent)
        for p in points:
            interface.insert_register(interface.create_register(p), base_topic)
        return interface
    return build


STANDARD_POINTS = [
    point('AI_2', 30, 2, 6, scaling=0.1, units='Volts', point_name='DGEN.VMinRtg'),
    point('AI_3', 30, 3, 6),
    point('AO_217', 40, 217, 4, writable=True, control_mode='sbo', default_value='1'),
    point('AO_218', 40, 218, 1, writable=True, scaling=10),
    point('BI_0', 1, 0, 2),
    point('BO_3', 10, 3, 2, writable=True, control_code='pulse', on_time=200, count=2),
    point('CTR_5000', 20, 5000, 1),
]


@pytest.fixture
def interface(make_interface):
    iface = make_interface(STANDARD_POINTS)
    iface.proxy_peer = iface.ppm.peer
    return iface


TOPIC = 'campus/building/der/{}'.format
IDENTITY = {'host': '10.0.0.5', 'port': 20000, 'master_address': 2, 'outstation_address': 1}
