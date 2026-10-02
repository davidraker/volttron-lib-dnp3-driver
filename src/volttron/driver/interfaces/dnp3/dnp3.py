# -*- coding: utf-8 -*- {{{
# ===----------------------------------------------------------------------===
#
#                 Installable Component of Eclipse VOLTTRON
#
# ===----------------------------------------------------------------------===
#
# Copyright 2025 Battelle Memorial Institute
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# ===----------------------------------------------------------------------===
# }}}
"""DNP3 (IEEE 1815) master interface for the VOLTTRON Platform Driver.

The protocol runs in a DNP3 Protocol Proxy process (``protocol_proxy.protocol.dnp3``, built on dnp3py); this interface
registers the outstation and its points with that proxy, asks it to read and operate points by their full topics, and
publishes the unsolicited responses it pushes back.
"""
from gevent import monkey
monkey.patch_socket()

import json
import logging

from collections import defaultdict
from typing import Any, Iterable, cast

from gevent import Timeout
from gevent.event import AsyncResult

from protocol_proxy.ipc import callback, ProtocolProxyMessage, ProtocolProxyPeer
from protocol_proxy.manager.gevent import GeventProtocolProxyManager

from volttron.driver.base.interfaces import BaseInterface, BaseRegister, BasicRevert, DriverInterfaceError

from .config import ControlMode, Dnp3PointConfig, Dnp3RemoteConfig, OUTPUT_GROUPS

_log = logging.getLogger(__name__)

PROXY_NAME = 'dnp3'
BIT_GROUPS = frozenset({1, 2, 3, 4, 10, 11})
COUNTER_GROUPS = frozenset({20, 21, 22})
TRUE_STRINGS = ('true', 't', 'on', 'yes', 'y', '1')
FALSE_STRINGS = ('false', 'f', 'off', 'no', 'n', '0', '')


class Dnp3Register(BaseRegister):
    """One point on an outstation: where it lives and how the proxy should read, scale and operate it."""

    def __init__(self, point: Dnp3PointConfig):
        # The base driver only distinguishes 'bit' from 'byte' to label booleans in publish metadata.
        super().__init__('bit' if point.group in BIT_GROUPS else 'byte', not point.writable, point.volttron_point_name,
                         point.units, description=point.description or point.notes)
        self.group = point.group
        self.variation = point.resolved_variation
        self.index = point.index
        self.scaling = point.scaling
        self.control_mode: ControlMode | None = point.control_mode
        self.control_code = point.control_code
        self.count, self.on_time, self.off_time = point.count, point.on_time, point.off_time
        self.python_type = bool if point.group in BIT_GROUPS else int if point.group in COUNTER_GROUPS else float
        self.default_value: Any = None     # Value to revert to, already coerced to python_type; None if unset.

    @property
    def is_output(self) -> bool:
        return self.group in OUTPUT_GROUPS

    def point_fields(self, topic: str) -> dict:
        """This register's entry in the REGISTER_OUTSTATION point table."""
        return {'topic': topic, 'group': self.group, 'variation': self.variation, 'index': self.index,
                'scaling': self.scaling, 'control_code': self.control_code.value, 'count': self.count,
                'on_time': self.on_time, 'off_time': self.off_time}

    def __repr__(self) -> str:
        return f'Dnp3Register({self.point_name!r}, g{self.group}v{self.variation} i{self.index})'


class Dnp3(BasicRevert, BaseInterface):
    """Platform Driver interface for a DNP3 outstation, served by the DNP3 Protocol Proxy."""

    REGISTER_CONFIG_CLASS = Dnp3PointConfig
    INTERFACE_CONFIG_CLASS = Dnp3RemoteConfig

    def __init__(self, config, *args, **kwargs):
        BaseInterface.__init__(self, config, *args, **kwargs)
        BasicRevert.__init__(self, **kwargs)
        self.config: Dnp3RemoteConfig
        self.ppm: GeventProtocolProxyManager = GeventProtocolProxyManager.get_manager(PROXY_NAME)
        self.proxy_peer: ProtocolProxyPeer | None = None
        # The manager is shared by every DNP3 interface instance and keeps the first registration of a callback, so
        # pushes for every outstation arrive at one instance's handler; it publishes by topic and never consults its
        # own point_map (the same pattern as the BACnet interface's RECEIVE_COV).
        self.ppm.register_callback(self.receive_unsolicited, 'RECEIVE_UNSOLICITED', provides_response=False)
        self.ppm.start()
        self.driver_agent.core.spawn(self.ppm.select_loop)

    # ---- registers --------------------------------------------------------------------------------------------
    def create_register(self, register_definition: Dnp3PointConfig) -> Dnp3Register:
        register = Dnp3Register(register_definition)
        if register_definition.writable and register_definition.default_value not in (None, ''):
            try:
                register.default_value = self._coerce(register, register_definition.default_value)
            except (TypeError, ValueError):
                _log.warning(f"Unable to set default value for {register.point_name}: bad default value"
                             f" {register_definition.default_value!r} in configuration. Using default revert method.")
        return register

    def insert_register(self, register: BaseRegister, base_topic: str):
        register = cast(Dnp3Register, register)
        super().insert_register(register, base_topic)
        if register.default_value is not None:
            self.set_default('/'.join([base_topic, register.point_name]), register.default_value)

    def finalize_setup(self, initial_setup: bool = False):
        self.proxy_peer = self.ppm.get_proxy(self.config.proxy_key())
        self.ppm.wait_peer_registered(self.proxy_peer, self.config.registration_timeout, self.register_outstation)

    def register_outstation(self):
        """Declare (or redeclare) the outstation and its full point table to the proxy."""
        payload = {**self.config.outstation_fields(), **self.config.connection_fields(),
                   'points': [register.point_fields(topic) for topic, register in self.point_map.items()]}
        response = self._send('REGISTER_OUTSTATION', payload)
        result, errors = self.parse_proxy_response(response, ['outstation'])
        if errors:
            _log.warning(f"Failed to register DNP3 outstation {self.config.outstation_ip}:{self.config.port}"
                         f" (address {self.config.outstation_id}) with the proxy: {errors}")
            return
        _log.info(f"DNP3 outstation {self.config.outstation_ip}:{self.config.port} registered with the proxy as"
                  f" {result.get('client')} with {result.get('points')} points"
                  f"{', unsolicited reporting on' if self.config.unsolicited else ''}.")

    # ---- unsolicited (push) -----------------------------------------------------------------------------------
    @callback
    def receive_unsolicited(self, _, raw_message: bytes):
        """Publish the values the proxy pushes for unsolicited responses, keyed by full point topic."""
        try:
            message = json.loads(raw_message.decode('utf8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            _log.warning(f'Undecodable RECEIVE_UNSOLICITED message from the DNP3 Proxy: {e}')
            return
        if error := message.get('error'):
            _log.warning(f'Error received with unsolicited DNP3 values: {error}')
        if result := message.get('result'):
            self.driver_agent.publish_push(result)

    # ---- reads ------------------------------------------------------------------------------------------------
    def get_point(self, topic: str, **kwargs):
        results, errors = self._read([topic], mode='points')
        if topic in results:
            return results[topic]
        message = f"Error reading point: {topic} --- {errors.get(topic, errors)}"
        _log.warning(message)
        raise RuntimeError(message)

    def _get_multiple_points(self, topics: Iterable[str], **kwargs) -> tuple[dict, dict]:
        return self._read(list(topics), mode=self.config.read_mode.value, classes=self.config.poll_classes)

    def _read(self, topics: list[str], mode: str, classes: list[int] | None = None) -> tuple[dict, dict]:
        if self.proxy_peer is None:
            raise DriverInterfaceError("DNP3 interface not initialized. No proxy peer available.")
        results, errors = {}, {}
        known = []
        for topic in topics:
            if topic in self.point_map:
                known.append(topic)
            else:
                errors[topic] = 'Point not configured on device.'
        if not known:
            return results, errors
        payload = {**self.config.outstation_fields(), 'mode': mode, 'classes': list(classes or [0]), 'topics': known}
        try:
            response = self._send('READ_POINTS', payload)
            values, request_errors = self.parse_proxy_response(response, known)
            for topic in known:
                register = cast(Dnp3Register, self.point_map[topic])
                entry = values.get(topic) if isinstance(values, dict) else None
                if isinstance(entry, dict) and 'value' in entry:
                    if entry.get('online', True):
                        results[topic] = self._coerce(register, entry['value'])
                    else:
                        errors[topic] = f"point offline (quality 0x{int(entry.get('quality', 0)):02x})"
                elif topic in request_errors:
                    errors[topic] = request_errors[topic]
                elif 'link' in request_errors:
                    errors[topic] = request_errors['link']
                else:
                    errors[topic] = 'No value returned by the DNP3 Proxy.'
        except Timeout as e:
            _log.warning(f'Request timed out polling {self.config.outstation_ip}: {e}')
            for topic in known:
                errors.setdefault(topic, f'Timeout waiting for DNP3 Proxy: {e}')
        except Exception as e:
            _log.warning(f'Unexpected error polling {self.config.outstation_ip}: {e}')
            for topic in known:
                errors.setdefault(topic, f'Unexpected error: {e}')
        return results, errors

    # ---- writes -----------------------------------------------------------------------------------------------
    def _set_point(self, topic: str, value: Any, **kwargs):
        results, errors = self._write_points([(topic, value)])
        if topic in errors:
            message = f"Error writing point: {topic} --- {errors[topic]}"
            _log.warning(message)
            raise RuntimeError(message)
        return results[topic]

    def set_multiple_points(self, topics_values, **kwargs):
        results, errors = self._write_points(list(topics_values))
        for topic in results:
            self._tracker.mark_dirty_point(topic)
        if errors:
            _log.warning(f'Errors encountered setting points: {errors}')
        return results, errors

    def _write_points(self, topics_values: list[tuple[str, Any]]) -> tuple[dict, dict]:
        """Operate outputs, one WRITE_POINTS per control mode (a point's own mode, else the outstation's)."""
        if self.proxy_peer is None:
            raise DriverInterfaceError("DNP3 interface not initialized. No proxy peer available.")
        results, errors = {}, {}
        batches: dict[ControlMode, list[tuple[str, Any]]] = defaultdict(list)
        for topic, value in topics_values:
            register = cast(Dnp3Register | None, self.point_map.get(topic))
            if register is None:
                errors[topic] = 'Point not configured on device.'
                continue
            if register.read_only or not register.is_output:
                errors[topic] = 'Trying to write to a point configured read only.'
                continue
            try:
                coerced = self._coerce(register, value)
            except (TypeError, ValueError) as e:
                errors[topic] = f'Unable to convert {value!r} for {topic}: {e}'
                continue
            batches[register.control_mode or self.config.control_mode].append((topic, coerced))
        for mode, items in batches.items():
            payload = {**self.config.outstation_fields(), 'control_mode': mode.value,
                       'operations': [{'topic': topic, 'value': value} for topic, value in items]}
            try:
                response = self._send('WRITE_POINTS', payload)
                result, request_errors = self.parse_proxy_response(response, [topic for topic, _ in items])
                for topic, value in items:
                    if isinstance(result, dict) and topic in result:
                        results[topic] = value     # the proxy reports the echoed status; the value is what we asked for
                    elif topic in request_errors:
                        errors[topic] = request_errors[topic]
                    elif 'link' in request_errors:
                        errors[topic] = request_errors['link']
                    else:
                        errors[topic] = 'Write not acknowledged by the DNP3 Proxy.'
            except Timeout as e:
                _log.warning(f'Request timed out writing to {self.config.outstation_ip}: {e}')
                for topic, _ in items:
                    errors.setdefault(topic, f'Timeout waiting for DNP3 Proxy: {e}')
            except Exception as e:
                _log.warning(f'Unexpected error writing to {self.config.outstation_ip}: {e}')
                for topic, _ in items:
                    errors.setdefault(topic, f'Unexpected error: {e}')
        return results, errors

    # ---- proxy transport --------------------------------------------------------------------------------------
    def _send(self, method_name: str, payload: dict):
        return self.ppm.send(self.proxy_peer, ProtocolProxyMessage(method_name=method_name,
                                                                   payload=json.dumps(payload).encode('utf8'),
                                                                   response_expected=True))

    def parse_proxy_response(self, response: Any, error_keys: Iterable[str]) -> tuple[Any, dict]:
        """Wait for and unpack a proxy reply: ``(result, errors)``. A failure to get a reply is reported against every
        key in ``error_keys``. A gevent Timeout waiting on the AsyncResult is left to propagate to the caller."""
        def failed(message: str) -> tuple[dict, dict]:
            return {}, {key: message for key in error_keys}

        if not isinstance(response, AsyncResult):
            return failed(f'Unable to send request to DNP3 Proxy (send returned {response!r}).')
        raw = response.get(timeout=self.config.resolved_reply_timeout)
        if not raw:
            return failed('Empty response from DNP3 Proxy.')
        try:
            payload = json.loads(raw.decode('utf8') if isinstance(raw, (bytes, bytearray)) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as e:
            return failed(f'Undecodable response from DNP3 Proxy: {e}')
        if not isinstance(payload, dict):
            return failed(f'Unexpected response from DNP3 Proxy: {payload!r}')
        if payload.get('status') == 'error':
            return failed(f"DNP3 Proxy {payload.get('method')} failed: {payload.get('error')}")
        errors = payload.get('error')
        return payload.get('result', {}), errors if isinstance(errors, dict) else {}

    # ---- helpers ----------------------------------------------------------------------------------------------
    @staticmethod
    def _coerce(register: Dnp3Register, value: Any):
        python_type = register.python_type
        if python_type is bool:
            if isinstance(value, str):
                text = value.strip().lower()
                if text in TRUE_STRINGS:
                    return True
                if text in FALSE_STRINGS:
                    return False
                raise ValueError(f'{value!r} is not a boolean')
            return bool(value)
        if python_type is int:
            return int(value, 0) if isinstance(value, str) else int(value)
        if isinstance(value, bool):
            raise TypeError('a boolean is not a number')
        return float(value)

    @classmethod
    def unique_remote_id(cls, config_name: str, config) -> tuple:
        """Identifies the outstation: one DriverAgent per outstation address at a host and port."""
        cfg = cls.INTERFACE_CONFIG_CLASS(**config.model_dump())
        return 'dnp3', cfg.outstation_ip, cfg.port, cfg.outstation_id
