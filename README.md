# VOLTTRON DNP3 Driver Interface

A DNP3 (IEEE 1815-2012) master interface for the modular VOLTTRON Platform Driver. The protocol runs in a separate
[DNP3 Protocol Proxy](https://github.com/eclipse-volttron/lib-protocol-proxy-dnp3) process built on the pure Python
[dnp3py](https://github.com/craigpnnl/dnp3py) library; this interface registers each outstation and its points with
that proxy, polls and operates points through it, and publishes the unsolicited responses it pushes back. No C
libraries are involved.

## Requirements

- `volttron-platform-driver` and `volttron-lib-base-driver` >= 2.0.0rc6
- `protocol-proxy-dnp3` >= 2.0.0rc0 (installed as a dependency), which brings `protocol-proxy` and `dnp3py`
- Python >= 3.11

## Quick start

Install the Platform Driver and this interface into the VOLTTRON environment, then store a device configuration and
its registry:

```shell
vctl install volttron-platform-driver --vip-identity platform.driver --start
pip install volttron-lib-dnp3-driver
vctl config store platform.driver dnp3.csv example-config/dnp3.csv --csv
vctl config store platform.driver devices/campus/building/der example-config/dnp3.config
```

## Device configuration

```json
{
    "remote_config": {
        "driver_type": "dnp3",
        "outstation_ip": "127.0.0.1",
        "port": 20000,
        "master_id": 2,
        "outstation_id": 1,
        "control_mode": "direct",
        "read_mode": "class",
        "poll_classes": [0],
        "integrity_poll_interval": 3600,
        "unsolicited": false
    },
    "registry_config": "config://dnp3.csv",
    "interval": 5,
    "timezone": "UTC",
    "publish_depth_first_all": true
}
```

`remote_config` (the older `driver_config` key and its `master_ip` entry are still accepted) takes:

| Key | Default | Meaning |
|---|---|---|
| `outstation_ip` (or `host`) | required | Outstation address. |
| `port` | 20000 | Outstation TCP port. |
| `master_id` (or `master_address`) | 2 | DNP3 link address of this master. |
| `outstation_id` (or `outstation_address`) | 1 | DNP3 link address of the outstation. One DriverAgent per `(outstation_ip, port, outstation_id)`. |
| `control_mode` | `direct` | How outputs are operated unless a point says otherwise: `direct` (DIRECT_OPERATE) or `sbo` (SELECT then OPERATE; OPERATE is sent only when the SELECT echo accepts every control). |
| `read_mode` | `class` | What a scheduled poll requests: `class` reads the data classes in `poll_classes`, `integrity` reads Class 0, 1, 2 and 3, `points` issues range reads of exactly the polled points. `get_point` always uses a range read. |
| `poll_classes` | `[0]` | Data classes read by a `class` poll. Class 0 is the static data. |
| `integrity_poll_interval` | 3600 | Seconds between integrity polls, which refresh every point and collect buffered events. 0 disables the periodic poll; one always runs when the connection opens. A due integrity poll replaces the next scheduled read. |
| `unsolicited` | false | Enable unsolicited reporting of `unsolicited_classes` (default `[1, 2, 3]`). Events are published as they arrive (see below). |
| `link_reset` | true | Send RESET_LINK_STATE when the connection opens. |
| `response_timeout` | 5 | Seconds the proxy waits for the outstation to answer one request. |
| `reply_timeout` | 3 x `response_timeout`, at least 30 | Seconds this interface waits for the proxy's reply. |
| `registration_timeout` | 30 | Seconds to wait for the proxy process to start. |
| `proxy_group` | unset | All outstations share one proxy process unless a group name is given. |

## Registry configuration

```csv
Point Name,Volttron Point Name,Group,Variation,Index,Scaling,Units,Writable,Control Mode,Control Code,Notes
DGEN.VMinRtg,AI_2,30,6,2,0.1,Volts,FALSE,,,Nameplate Minimum Voltage Rating
DVVR.VVArCrv,AO_217,40,4,217,1,,TRUE,sbo,,Volt-VAr curve edit selector
DOPR.PermOp,BO_3,10,2,3,,,TRUE,,latch,Permit service
DGEN.WHrtg,CTR_5000,20,1,5000,1,Wh,FALSE,,,Energy counter
```

| Column | Meaning |
|---|---|
| `Volttron Point Name` | The point's name on the platform. Required. |
| `Point Name` | The point's name on the outstation (an IEC 61850 object in a MESA profile). Informational. |
| `Group`, `Variation`, `Index` | The point's DNP3 object group, variation and index. Groups read: 1, 2 (binary inputs), 3, 4 (double-bit inputs), 10, 11 (binary outputs), 20, 21, 22 (counters), 30, 32 (analog inputs), 40, 42 (analog outputs). A blank variation takes the usual one for the group (g1v2, g10v2, g20v1, g30v6, g40v4). For a writable analog output the registered group 40 variation also picks the group 41 command variation (1 INT32, 2 INT16, 3 FLT32, 4 FLT64). |
| `Scaling` | Multiplier applied by the proxy: published value = outstation value x scaling; writes divide by it. Blank is 1. |
| `Units` | Units reported in the publish metadata. |
| `Writable` | TRUE for an output the platform may operate. Only groups 10 and 40 may be writable. |
| `Control Mode` | Per-point override of the outstation's `control_mode`: `direct` or `sbo`. |
| `Control Code` | For binary outputs: `latch` (default; true latches on, false latches off) or `pulse` (true pulses on, false pulses off) with `On Time`, `Off Time` (milliseconds) and `Count`. |
| `Default Value` | The value `revert` writes when the point has no clean value yet. |
| `Transform` | Accepted for compatibility with the discovery tooling; not applied (see below). |
| `Notes` | Free text. |

`interoperability.discovery.dnp3` in the interoperability service writes registries in this format from an IEEE 1815.2
device profile.

## Behaviour

Points are published with their platform type: binary groups as booleans, counters as integers, analog groups as
floats. A point whose quality lacks the ONLINE flag is reported as an error for that poll rather than as a stale value.
Unknown indexes, refused controls (the status the outstation echoed, such as `status NOT_SUPPORTED`) and link failures
are reported per point in the poll's error map; `set_point` raises on them.

With `unsolicited` true the proxy listens for events between requests and pushes them to this interface, which
publishes them through the driver's push path (the same mechanism as the BACnet interface's change-of-value
subscriptions): the depth-first and breadth-first point topics, and the device's `multi` topics, per the publish
settings in force. The base driver's `data_source` column is not yet wired to poll scheduling, so a point that
receives unsolicited values is still read on the regular schedule as well.

`Transform` and `Scaling` both describe the raw-to-engineering conversion. Scaling is applied by the proxy; the
`Transform` expression is not yet applied by any interface and belongs in the base driver.

## Testing

```shell
pytest tests
```

The unit tests need no proxy, platform or outstation. `tests/test_end_to_end.py` starts dnp3py's own outstation on the
loopback interface and runs the real interface, proxy manager and proxy subprocess against it; it is skipped when
dnp3py is not installed.

## Development

This library is maintained by the VOLTTRON Development Team. Please see the
[guidelines](https://github.com/eclipse-volttron/volttron-core/blob/develop/CONTRIBUTING.md) for contributing to this
and other VOLTTRON repositories.

# Disclaimer Notice

This material was prepared as an account of work sponsored by an agency of the
United States Government.  Neither the United States Government nor the United
States Department of Energy, nor Battelle, nor any of their employees, nor any
jurisdiction or organization that has cooperated in the development of these
materials, makes any warranty, express or implied, or assumes any legal
liability or responsibility for the accuracy, completeness, or usefulness or any
information, apparatus, product, software, or process disclosed, or represents
that its use would not infringe privately owned rights.

Reference herein to any specific commercial product, process, or service by
trade name, trademark, manufacturer, or otherwise does not necessarily
constitute or imply its endorsement, recommendation, or favoring by the United
States Government or any agency thereof, or Battelle Memorial Institute. The
views and opinions of authors expressed herein do not necessarily state or
reflect those of the United States Government or any agency thereof.
