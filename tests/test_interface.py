"""Unit tests for the DNP3 interface: configuration, registers, and every message to and from the proxy."""
import logging

import pytest

from gevent import Timeout
from pydantic import ValidationError

from volttron.driver.base.config import RemoteConfig
from volttron.driver.base.interfaces import DriverInterfaceError
from volttron.driver.interfaces.dnp3.config import ControlCode, ControlMode, Dnp3PointConfig, Dnp3RemoteConfig, ReadMode
from volttron.driver.interfaces.dnp3.dnp3 import Dnp3

from tests.conftest import IDENTITY, STANDARD_POINTS, TOPIC, point, reading, serialized


class TestPointConfig:
    def test_csv_row_as_the_discovery_tool_writes_it(self):
        row = {'Point Name': 'DGEN.VMinRtg', 'Volttron Point Name': 'AI_2', 'Group': '30', 'Variation': '6', 'Index': '2',
               'Scaling': '0.1', 'Units': 'Volts', 'Writable': 'FALSE', 'Transform': 'scale(0.1)', 'Notes': 'Nameplate'}
        cfg = Dnp3PointConfig(**row)
        assert (cfg.point_name, cfg.volttron_point_name, cfg.group, cfg.variation, cfg.index) == ('DGEN.VMinRtg', 'AI_2', 30, 6, 2)
        assert cfg.scaling == 0.1 and cfg.writable is False and cfg.transform == 'scale(0.1)' and cfg.notes == 'Nameplate'
        assert cfg.control_mode is None and cfg.control_code is ControlCode.latch and cfg.resolved_variation == 6

    def test_blank_optional_columns_take_defaults(self):
        cfg = Dnp3PointConfig(**{'Volttron Point Name': 'BO_3', 'Group': '10', 'Variation': '', 'Index': '3', 'Scaling': '',
                                 'Writable': 'TRUE', 'Control Mode': '', 'Control Code': '', 'On Time': '', 'Count': ''})
        assert cfg.variation is None and cfg.resolved_variation == 2 and cfg.scaling == 1.0
        assert cfg.control_mode is None and cfg.control_code is ControlCode.latch and cfg.on_time == 0 and cfg.count == 1

    def test_control_columns(self):
        cfg = Dnp3PointConfig(**{'Volttron Point Name': 'AO_1', 'Group': 40, 'Index': 1, 'Writable': True,
                                 'Control Mode': 'SBO', 'Control Code': 'Pulse', 'On Time': 250, 'Off Time': 50, 'Count': 3})
        assert cfg.control_mode is ControlMode.sbo and cfg.control_code is ControlCode.pulse
        assert (cfg.on_time, cfg.off_time, cfg.count) == (250, 50, 3)
        assert Dnp3PointConfig(volttron_point_name='x', group=40, index=1, multiplier='0.5').scaling == 0.5

    def test_rejections(self):
        with pytest.raises(ValidationError, match='not one the DNP3 driver reads'):
            Dnp3PointConfig(volttron_point_name='x', group=12, index=0)
        with pytest.raises(ValidationError, match='not an output and cannot be writable'):
            Dnp3PointConfig(volttron_point_name='x', group=30, index=0, writable=True)
        with pytest.raises(ValidationError):
            Dnp3PointConfig(volttron_point_name='x', group=10, index=0, writable=True, control_mode='maybe')
        with pytest.raises(ValidationError):
            Dnp3PointConfig(volttron_point_name='x', group=10, index=0, count=0)


class TestRemoteConfig:
    def test_old_driver_config_keys_still_work(self):
        cfg = Dnp3RemoteConfig(driver_type='dnp3', master_ip='0.0.0.0', outstation_ip='127.0.0.1', master_id=2,
                               outstation_id=1, port=20000)
        assert cfg.outstation_fields() == {'host': '127.0.0.1', 'port': 20000, 'master_address': 2, 'outstation_address': 1}
        assert cfg.master_ip == '0.0.0.0'      # accepted, unused

    def test_defaults_aliases_and_derived_values(self):
        cfg = Dnp3RemoteConfig(driver_type='dnp3', host='10.0.0.5', master_address=7, outstation_address=4,
                               control_mode='SBO', read_mode='Integrity', poll_classes='0 1', unsolicited_classes=[3, 1])
        assert cfg.outstation_ip == '10.0.0.5' and cfg.master_id == 7 and cfg.outstation_id == 4 and cfg.port == 20000
        assert cfg.control_mode is ControlMode.sbo and cfg.read_mode is ReadMode.integrity
        assert cfg.poll_classes == [0, 1] and cfg.unsolicited_classes == [1, 3]
        assert cfg.integrity_poll_interval == 3600.0 and cfg.unsolicited is False and cfg.link_reset is True
        assert cfg.resolved_reply_timeout == 30.0 and Dnp3RemoteConfig(driver_type='dnp3', host='h', response_timeout=20).resolved_reply_timeout == 60.0
        assert cfg.proxy_key() == ('dnp3',) and Dnp3RemoteConfig(driver_type='dnp3', host='h', proxy_group='site').proxy_key() == ('dnp3', 'site')
        assert cfg.connection_fields() == {'response_timeout': 5.0, 'link_reset': True, 'integrity_poll_interval': 3600.0,
                                           'unsolicited': False, 'unsolicited_classes': [1, 3]}

    def test_rejections(self):
        with pytest.raises(ValidationError):
            Dnp3RemoteConfig(driver_type='dnp3')
        with pytest.raises(ValidationError, match='classes are 0 to 3'):
            Dnp3RemoteConfig(driver_type='dnp3', host='h', poll_classes=[4])
        with pytest.raises(ValidationError):
            Dnp3RemoteConfig(driver_type='dnp3', host='h', integrity_poll_interval=-1)


class TestRegisters:
    def test_types_and_point_fields(self, interface):
        ai, ao, bi, bo, ctr = (interface.point_map[TOPIC(n)] for n in ('AI_2', 'AO_218', 'BI_0', 'BO_3', 'CTR_5000'))
        assert (ai.python_type, ao.python_type, bi.python_type, bo.python_type, ctr.python_type) == (float, float, bool, bool, int)
        assert (ai.register_type, bi.register_type, bo.register_type) == ('byte', 'bit', 'bit')
        assert ai.read_only and not ao.read_only and ao.is_output and not ai.is_output
        assert ai.description == '' and ai.units == 'Volts'
        assert bo.point_fields(TOPIC('BO_3')) == {'topic': TOPIC('BO_3'), 'group': 10, 'variation': 2, 'index': 3,
                                                  'scaling': 1.0, 'control_code': 'pulse', 'count': 2, 'on_time': 200,
                                                  'off_time': 0}
        assert interface.get_register_names() == [TOPIC(p.volttron_point_name) for p in STANDARD_POINTS]

    def test_default_value_is_coerced_and_tracked_by_topic(self, interface):
        assert interface.point_map[TOPIC('AO_217')].default_value == 1.0
        assert interface._tracker.defaults[TOPIC('AO_217')] == 1.0

    def test_bad_default_value_is_warned_and_ignored(self, make_interface, caplog):
        with caplog.at_level(logging.WARNING):
            iface = make_interface([point('AO_1', 40, 1, writable=True, default_value='lots')])
        assert iface.point_map[TOPIC('AO_1')].default_value is None and 'bad default value' in caplog.text


class TestSetup:
    def test_constructor_wires_the_shared_manager(self, interface, ppm):
        assert ppm.started == 1 and 'RECEIVE_UNSOLICITED' in ppm.callbacks
        interface.driver_agent.core.spawn.assert_called_once_with(ppm.select_loop)

    def test_finalize_setup_launches_the_proxy_and_registers_the_outstation(self, make_interface, ppm):
        iface = make_interface(STANDARD_POINTS, integrity_poll_interval=600, unsolicited=True, unsolicited_classes=[1])
        ppm.queue(serialized({'client': '10.0.0.5:20000:1:2', 'points': 7}))
        iface.finalize_setup(initial_setup=True)
        assert ppm.launch == (('dnp3',), {}) and ppm.registration_waits == [30.0] and iface.proxy_peer is ppm.peer
        (method, payload, expects_reply), = ppm.sent
        assert method == 'REGISTER_OUTSTATION' and expects_reply
        assert payload == {**IDENTITY, 'response_timeout': 5.0, 'link_reset': True, 'integrity_poll_interval': 600.0,
                           'unsolicited': True, 'unsolicited_classes': [1],
                           'points': [iface.point_map[t].point_fields(t) for t in iface.point_map]}
        assert payload['points'][0] == {'topic': TOPIC('AI_2'), 'group': 30, 'variation': 6, 'index': 2, 'scaling': 0.1,
                                        'control_code': 'latch', 'count': 1, 'on_time': 0, 'off_time': 0}

    def test_registration_failure_is_logged(self, interface, ppm, caplog):
        ppm.queue(serialized({}, {'outstation': 'group 99 is not a point the proxy reads'}))
        with caplog.at_level(logging.WARNING):
            interface.finalize_setup()
        assert 'Failed to register' in caplog.text and 'DNP3 Proxy' in caplog.text

    def test_proxy_group_selects_the_process(self, make_interface, ppm):
        iface = make_interface(proxy_group='plant-a')
        iface.finalize_setup()
        assert ppm.launch[0] == ('dnp3', 'plant-a')

    def test_unique_remote_id(self):
        remote = RemoteConfig(driver_type='dnp3', outstation_ip='10.0.0.5', port=20001, outstation_id=3)
        assert Dnp3.unique_remote_id('devices/x', remote) == ('dnp3', '10.0.0.5', 20001, 3)

    def test_reads_before_setup_raise(self, make_interface):
        iface = make_interface(STANDARD_POINTS)
        with pytest.raises(DriverInterfaceError):
            iface.get_multiple_points([TOPIC('AI_2')])


class TestReads:
    def test_poll_sends_the_configured_mode_and_coerces_values(self, interface, ppm):
        ppm.queue(serialized({TOPIC('AI_2'): reading(240.1), TOPIC('BI_0'): reading(True), TOPIC('CTR_5000'): reading(42),
                              TOPIC('AO_218'): reading(1200.0)}))
        results, errors = interface.get_multiple_points([TOPIC('AI_2'), TOPIC('BI_0'), TOPIC('CTR_5000'), TOPIC('AO_218')])
        assert ppm.payloads('READ_POINTS') == [{**IDENTITY, 'mode': 'class', 'classes': [0],
                                                'topics': [TOPIC('AI_2'), TOPIC('BI_0'), TOPIC('CTR_5000'), TOPIC('AO_218')]}]
        assert results == {TOPIC('AI_2'): 240.1, TOPIC('BI_0'): True, TOPIC('CTR_5000'): 42, TOPIC('AO_218'): 1200.0}
        assert isinstance(results[TOPIC('CTR_5000')], int) and errors == {}

    def test_read_mode_and_classes_come_from_the_remote(self, make_interface, ppm):
        iface = make_interface(STANDARD_POINTS, read_mode='integrity', poll_classes=[0, 1])
        iface.proxy_peer = ppm.peer
        ppm.queue(serialized({TOPIC('AI_2'): reading(1)}))
        iface.get_multiple_points([TOPIC('AI_2')])
        assert ppm.payloads('READ_POINTS')[0]['mode'] == 'integrity' and ppm.payloads('READ_POINTS')[0]['classes'] == [0, 1]

    def test_get_point_reads_that_point_by_range(self, interface, ppm):
        ppm.queue(serialized({TOPIC('AI_3'): reading(5000)}))
        assert interface.get_point(TOPIC('AI_3')) == 5000.0
        assert ppm.payloads('READ_POINTS') == [{**IDENTITY, 'mode': 'points', 'classes': [0], 'topics': [TOPIC('AI_3')]}]
        ppm.queue(serialized({}, {TOPIC('AI_3'): 'not reported by outstation'}))
        with pytest.raises(RuntimeError, match='not reported by outstation'):
            interface.get_point(TOPIC('AI_3'))

    def test_offline_points_and_missing_values_are_errors(self, interface, ppm):
        ppm.queue(serialized({TOPIC('AI_2'): reading(3.0, quality=0x02, online=False)}, {TOPIC('BI_0'): 'not reported by outstation'}))
        results, errors = interface.get_multiple_points([TOPIC('AI_2'), TOPIC('BI_0'), TOPIC('AI_3'), 'devices/nope'])
        assert results == {}
        assert errors == {TOPIC('AI_2'): 'point offline (quality 0x02)', TOPIC('BI_0'): 'not reported by outstation',
                          TOPIC('AI_3'): 'No value returned by the DNP3 Proxy.', 'devices/nope': 'Point not configured on device.'}

    def test_link_error_applies_to_every_point(self, interface, ppm):
        ppm.queue(serialized({}, {'link': 'LinkError: Peer closed the connection'}))
        results, errors = interface.get_multiple_points([TOPIC('AI_2'), TOPIC('BI_0')])
        assert results == {} and set(errors.values()) == {'LinkError: Peer closed the connection'}

    def test_proxy_failures(self, interface, ppm):
        ppm.queue(False)
        _, errors = interface.get_multiple_points([TOPIC('AI_2')])
        assert 'Unable to send request' in errors[TOPIC('AI_2')]
        ppm.queue(b'')
        _, errors = interface.get_multiple_points([TOPIC('AI_2')])
        assert errors[TOPIC('AI_2')] == 'Empty response from DNP3 Proxy.'
        ppm.queue(b'{"status": "error", "error": "Operation timed out", "method": "READ_POINTS"}')
        _, errors = interface.get_multiple_points([TOPIC('AI_2')])
        assert errors[TOPIC('AI_2')] == 'DNP3 Proxy READ_POINTS failed: Operation timed out'

    def test_reply_timeout_is_reported_per_point(self, interface, ppm, monkeypatch):
        def never(peer, message):
            raise Timeout()
        monkeypatch.setattr(ppm, 'send', never)
        results, errors = interface.get_multiple_points([TOPIC('AI_2'), TOPIC('BI_0')])
        assert results == {} and all(e.startswith('Timeout waiting for DNP3 Proxy') for e in errors.values())


class TestWrites:
    def test_set_point_sends_direct_operate_by_default(self, interface, ppm):
        ppm.queue(serialized({TOPIC('AO_218'): {'status': 'SUCCESS', 'value': 1200.0}}))
        assert interface.set_point(TOPIC('AO_218'), '1200') == 1200.0
        assert ppm.payloads('WRITE_POINTS') == [{**IDENTITY, 'control_mode': 'direct',
                                                 'operations': [{'topic': TOPIC('AO_218'), 'value': 1200.0}]}]

    def test_per_point_control_mode_splits_the_writes(self, interface, ppm):
        ppm.queue(serialized({TOPIC('AO_218'): {'status': 'SUCCESS', 'value': 5.0}, TOPIC('BO_3'): {'status': 'SUCCESS', 'value': True}}),
                  serialized({TOPIC('AO_217'): {'status': 'SUCCESS', 'value': 2.0}}))
        results, errors = interface.set_multiple_points([(TOPIC('AO_218'), 5), (TOPIC('AO_217'), 2), (TOPIC('BO_3'), 'on')])
        assert errors == {} and results == {TOPIC('AO_218'): 5.0, TOPIC('AO_217'): 2.0, TOPIC('BO_3'): True}
        modes = [(p['control_mode'], [o['topic'] for o in p['operations']]) for p in ppm.payloads('WRITE_POINTS')]
        assert modes == [('direct', [TOPIC('AO_218'), TOPIC('BO_3')]), ('sbo', [TOPIC('AO_217')])]

    def test_outstation_default_mode_applies_to_points_without_their_own(self, make_interface, ppm):
        iface = make_interface(STANDARD_POINTS, control_mode='sbo')
        iface.proxy_peer = ppm.peer
        ppm.queue(serialized({TOPIC('BO_3'): {'status': 'SUCCESS', 'value': False}}))
        iface.set_point(TOPIC('BO_3'), False)
        assert ppm.payloads('WRITE_POINTS')[0]['control_mode'] == 'sbo'

    def test_echoed_failure_status_and_local_rejections(self, interface, ppm):
        ppm.queue(serialized({}, {TOPIC('AO_218'): 'status NOT_SUPPORTED'}))
        results, errors = interface.set_multiple_points([(TOPIC('AO_218'), 1), (TOPIC('AI_2'), 1), (TOPIC('BO_3'), 'maybe'),
                                                         ('devices/nope', 1)])
        assert results == {}
        assert errors[TOPIC('AO_218')] == 'status NOT_SUPPORTED'
        assert errors[TOPIC('AI_2')] == 'Trying to write to a point configured read only.'
        assert errors[TOPIC('BO_3')].startswith('Unable to convert') and errors['devices/nope'] == 'Point not configured on device.'
        assert ppm.payloads('WRITE_POINTS')[0]['operations'] == [{'topic': TOPIC('AO_218'), 'value': 1.0}]
        with pytest.raises(RuntimeError, match='read only'):
            interface.set_point(TOPIC('AI_2'), 1)

    def test_unacknowledged_write(self, interface, ppm):
        ppm.queue(serialized({}))
        _, errors = interface.set_multiple_points([(TOPIC('AO_218'), 1)])
        assert errors == {TOPIC('AO_218'): 'Write not acknowledged by the DNP3 Proxy.'}

    def test_revert_uses_the_default_value(self, interface, ppm):
        ppm.queue(serialized({TOPIC('AO_217'): {'status': 'SUCCESS', 'value': 1.0}}))
        interface.revert_point(TOPIC('AO_217'))
        assert ppm.payloads('WRITE_POINTS') == [{**IDENTITY, 'control_mode': 'sbo',
                                                 'operations': [{'topic': TOPIC('AO_217'), 'value': 1.0}]}]


class TestUnsolicited:
    def test_receive_push_publishes_pushed_values(self, interface, driver_agent):
        raw = serialized({TOPIC('AI_2'): 241.5, 'campus/other/der/BI_0': True})
        interface.receive_push.__wrapped__(interface, None, raw)
        driver_agent.publish_push.assert_called_once_with({TOPIC('AI_2'): 241.5, 'campus/other/der/BI_0': True})

    def test_receive_push_logs_errors_and_publishes_nothing(self, interface, driver_agent, caplog):
        with caplog.at_level(logging.WARNING):
            interface.receive_push.__wrapped__(interface, None, serialized({}, {'link': 'lost'}))
            interface.receive_push.__wrapped__(interface, None, b'not json')
        assert 'Error received with pushed values from the DNP3 Proxy' in caplog.text and 'Undecodable' in caplog.text
        assert not driver_agent.publish_push.called
