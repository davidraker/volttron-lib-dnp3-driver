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
publishes the unsolicited responses it pushes back. The exchange itself (registration, reply parsing, batching, pushes,
failure handling) is :class:`ProxyBackedInterface`; this module holds what is DNP3: the point table, the read modes, the
control modes, quality handling and value coercion.
"""
from gevent import monkey
monkey.patch_socket()

import logging

from collections import defaultdict
from typing import Any, cast

from volttron.driver.base.interfaces import BaseInterface, BaseRegister, BasicRevert
from volttron.driver.base.proxy_interface import PointError, ProxyBackedInterface

from .config import ControlMode, Dnp3PointConfig, Dnp3RemoteConfig, OUTPUT_GROUPS

_log = logging.getLogger(__name__)

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


class Dnp3(ProxyBackedInterface, BasicRevert, BaseInterface):
    """Platform Driver interface for a DNP3 outstation, served by the DNP3 Protocol Proxy."""

    REGISTER_CONFIG_CLASS = Dnp3PointConfig
    INTERFACE_CONFIG_CLASS = Dnp3RemoteConfig
    PROXY_NAME, PROXY_LABEL = 'dnp3', 'DNP3 Proxy'
    REGISTER_METHOD, READ_METHOD, WRITE_METHOD = 'REGISTER_OUTSTATION', 'READ_POINTS', 'WRITE_POINTS'
    PUSH_METHOD = 'RECEIVE_UNSOLICITED'
    REQUEST_ERROR_KEY = 'link'

    def __init__(self, config, *args, **kwargs):
        BaseInterface.__init__(self, config, *args, **kwargs)
        BasicRevert.__init__(self, **kwargs)
        self.config: Dnp3RemoteConfig
        self.init_proxy()

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

    # ---- the proxy exchange, in DNP3 terms ----------------------------------------------------------------------
    def identity_fields(self) -> dict:
        return self.config.outstation_fields()

    def registration_payload(self) -> dict:
        return {**self.config.outstation_fields(), **self.config.connection_fields(),
                'points': [register.point_fields(topic) for topic, register in self.point_map.items()]}

    def after_registration(self, result: dict, initial_setup: bool):
        _log.info(f"DNP3 outstation {self.config.outstation_ip}:{self.config.port} registered with the proxy as"
                  f" {result.get('client')} with {result.get('points')} points"
                  f"{', unsolicited reporting on' if self.config.unsolicited else ''}.")

    def read_payload(self, topics: list[str], mode: str | None = None, classes=None, **kwargs) -> dict:
        """``mode`` is class (the remote's ``poll_classes``), integrity or points; polls use the remote's read mode."""
        return {'mode': mode or self.config.read_mode.value, 'classes': list(classes or self.config.poll_classes),
                'topics': list(topics)}

    def get_point(self, topic: str, **kwargs):
        kwargs.setdefault('mode', 'points')                     # one range read for the point asked for
        return super().get_point(topic, **kwargs)

    def read_result(self, register: BaseRegister, entry: Any) -> Any:
        if isinstance(entry, dict) and 'value' in entry:
            if entry.get('online', True):
                return self._coerce(cast(Dnp3Register, register), entry['value'])
            raise PointError(f"point offline (quality 0x{int(entry.get('quality', 0)):02x})")
        raise PointError('No value returned by the DNP3 Proxy.')

    def coerce(self, register: BaseRegister, value: Any) -> Any:
        return self._coerce(cast(Dnp3Register, register), value)

    def split_writes(self, items: list[tuple[str, Any]], **kwargs) -> list[tuple[dict, list[tuple[str, Any]]]]:
        """One WRITE_POINTS per control mode: a point's own mode, else the outstation's."""
        batches: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        for topic, value in items:
            register = cast(Dnp3Register, self.point_map[topic])
            batches[(register.control_mode or self.config.control_mode).value].append((topic, value))
        return [({'control_mode': mode, 'operations': [{'topic': topic, 'value': value} for topic, value in batch]}, batch)
                for mode, batch in batches.items()]

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
