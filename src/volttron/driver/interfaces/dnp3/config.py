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
"""Configuration models for the DNP3 driver interface: one registry row and one outstation.

Kept free of protocol_proxy imports so configuration tooling can validate registries without the proxy installed.
"""
from enum import Enum
from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator

from volttron.driver.base.config import PointConfig, RemoteConfig


class ControlMode(str, Enum):
    """How an output is operated: DIRECT_OPERATE in one step, or SELECT then OPERATE (sbo)."""
    direct = 'direct'
    sbo = 'sbo'


class ControlCode(str, Enum):
    """The control relay output block a binary output receives: latch on/off, or pulse on/off with timing."""
    latch = 'latch'
    pulse = 'pulse'


class ReadMode(str, Enum):
    """What a scheduled poll asks the outstation for."""
    class_poll = 'class'        # the data classes in poll_classes; Class 0 is the static data
    integrity = 'integrity'     # Class 0, 1, 2 and 3
    points = 'points'           # range reads of exactly the polled points


#: Object groups the proxy reads (static and event groups of inputs, outputs and counters).
SUPPORTED_GROUPS = frozenset({1, 2, 3, 4, 10, 11, 20, 21, 22, 30, 32, 40, 42})
#: Groups a master may operate: binary outputs (g12 commands) and analog outputs (g41 commands).
OUTPUT_GROUPS = frozenset({10, 40})
#: Variation assumed when a registry row leaves it blank.
DEFAULT_VARIATIONS = {1: 2, 2: 2, 3: 2, 4: 2, 10: 2, 11: 2, 20: 1, 21: 1, 22: 1, 30: 6, 32: 6, 40: 4, 42: 6}


def _blank_to_none(value):
    return None if isinstance(value, str) and value.strip() == '' else value


class Dnp3PointConfig(PointConfig):
    """One registry row. Column headers as the discovery tooling writes them are accepted alongside snake_case."""
    # The point's name on the outstation (an IEC 61850 object such as DGEN.VMinRtg in a MESA profile); informational.
    point_name: str = Field(default='', validation_alias=AliasChoices('point_name', 'Point Name'))
    group: int = Field(validation_alias=AliasChoices('group', 'Group'))
    variation: int | None = Field(default=None, validation_alias=AliasChoices('variation', 'Variation'))
    index: int = Field(validation_alias=AliasChoices('index', 'Index'))
    # Engineering value = outstation value * scaling; writes divide by it. The proxy applies it.
    scaling: float = Field(default=1.0, validation_alias=AliasChoices('scaling', 'Scaling', 'multiplier', 'Multiplier'))
    # Per-point override of the outstation's control_mode.
    control_mode: ControlMode | None = Field(default=None, validation_alias=AliasChoices('control_mode', 'Control Mode'))
    control_code: ControlCode = Field(default=ControlCode.latch, validation_alias=AliasChoices('control_code', 'Control Code'))
    count: int = Field(default=1, ge=1, validation_alias=AliasChoices('count', 'Count'))
    on_time: int = Field(default=0, ge=0, validation_alias=AliasChoices('on_time', 'On Time'))      # milliseconds
    off_time: int = Field(default=0, ge=0, validation_alias=AliasChoices('off_time', 'Off Time'))   # milliseconds
    default_value: Any = Field(default=None, validation_alias=AliasChoices('default_value', 'Default Value'))
    description: str = Field(default='', validation_alias=AliasChoices('description', 'Description'))
    # TODO: transform is not yet implemented; it should be handled by the base driver for all interfaces.
    transform: str = Field(default='', validation_alias=AliasChoices('transform', 'Transform'))

    @model_validator(mode='before')
    @classmethod
    def _blank_optionals(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for key in list(data):
                if key in ('variation', 'Variation', 'scaling', 'Scaling', 'multiplier', 'Multiplier', 'control_mode',
                           'Control Mode', 'control_code', 'Control Code', 'count', 'Count', 'on_time', 'On Time',
                           'off_time', 'Off Time', 'default_value', 'Default Value') and _blank_to_none(data[key]) is None:
                    del data[key]
        return data

    @field_validator('control_mode', 'control_code', mode='before')
    @classmethod
    def _lower(cls, v):
        return v.lower().strip() if isinstance(v, str) else v

    @model_validator(mode='after')
    def _check_group(self):
        if self.group not in SUPPORTED_GROUPS:
            raise ValueError(f'Point {self.volttron_point_name}: group {self.group} is not one the DNP3 driver reads'
                             f' ({sorted(SUPPORTED_GROUPS)}).')
        if self.writable and self.group not in OUTPUT_GROUPS:
            raise ValueError(f'Point {self.volttron_point_name}: group {self.group} is not an output and cannot be writable.')
        return self

    @property
    def resolved_variation(self) -> int:
        return self.variation if self.variation else DEFAULT_VARIATIONS.get(self.group, 1)


class Dnp3RemoteConfig(RemoteConfig):
    """One outstation. Keys from the previous driver (`master_ip`, `outstation_ip`, `master_id`, `outstation_id`) are
    accepted, so existing device configurations keep working."""
    outstation_ip: str = Field(validation_alias=AliasChoices('outstation_ip', 'host', 'device_address'))
    port: int = 20000
    master_id: int = Field(default=2, validation_alias=AliasChoices('master_id', 'master_address'))
    outstation_id: int = Field(default=1, validation_alias=AliasChoices('outstation_id', 'outstation_address'))
    # Accepted from old configurations and not used: dnp3py does not bind a local address.
    master_ip: str | None = None
    control_mode: ControlMode = ControlMode.direct
    read_mode: ReadMode = ReadMode.class_poll
    poll_classes: list[int] = [0]
    # Seconds between integrity polls; 0 disables them, one always runs when the connection opens.
    integrity_poll_interval: float = Field(default=3600.0, ge=0)
    unsolicited: bool = False
    unsolicited_classes: list[int] = [1, 2, 3]
    link_reset: bool = True
    # How long the proxy waits for the outstation to answer one request.
    response_timeout: float = Field(default=5.0, gt=0)
    # How long this interface waits for the proxy's reply; defaults to three response timeouts, at least 30 s.
    reply_timeout: float | None = Field(default=None, gt=0)
    registration_timeout: float = Field(default=30.0, gt=0)
    # All outstations share one proxy process unless a group is named here.
    proxy_group: str | None = None

    @field_validator('control_mode', 'read_mode', mode='before')
    @classmethod
    def _lower(cls, v):
        return v.lower().strip() if isinstance(v, str) else v

    @field_validator('poll_classes', 'unsolicited_classes', mode='before')
    @classmethod
    def _classes(cls, v):
        if isinstance(v, (int, str)):
            v = [int(c) for c in str(v).replace(',', ' ').split()]
        classes = sorted({int(c) for c in v})
        if any(c not in (0, 1, 2, 3) for c in classes):
            raise ValueError(f'DNP3 data classes are 0 to 3, got {classes}')
        return classes

    @property
    def resolved_reply_timeout(self) -> float:
        return self.reply_timeout if self.reply_timeout is not None else max(30.0, 3 * self.response_timeout)

    def proxy_key(self) -> tuple:
        """Selects the proxy process. Constant by default so all outstations share one."""
        return ('dnp3',) if self.proxy_group is None else ('dnp3', self.proxy_group)

    def outstation_fields(self) -> dict:
        """The identity sent with every message to the proxy."""
        return {'host': self.outstation_ip, 'port': self.port, 'master_address': self.master_id,
                'outstation_address': self.outstation_id}

    def connection_fields(self) -> dict:
        """Connection and polling settings sent with REGISTER_OUTSTATION."""
        return {'response_timeout': self.response_timeout, 'link_reset': self.link_reset,
                'integrity_poll_interval': self.integrity_poll_interval, 'unsolicited': self.unsolicited,
                'unsolicited_classes': list(self.unsolicited_classes)}
