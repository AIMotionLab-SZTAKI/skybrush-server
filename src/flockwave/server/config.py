"""Default configuration for the Skybrush server.

This script will be evaluated first when the server attempts to load its
configuration. Configuration files may import variables from this module
with `from flockwave.server.config import SOMETHING`, and may also modify
them if the variables are mutable. For instance, to disable an extension
locally, create a configuration file containing this:

    from flockwave.server.config import EXTENSIONS
    del EXTENSIONS["extension_to_disable"]
"""

import platform

on_mac = platform.system().lower() == "darwin"

# Label that is used to identify the server in SSDP discovery requests
SERVER_NAME = "Skybrush server"

# Configure the command execution manager
COMMAND_EXECUTION_MANAGER = {"timeout": 90}

# Declare the list of extensions to load
EXTENSIONS = {
    "auth": {},
    "auth_basic": {"enabled": False},
    "beacon": {"enabled": False},
    "console_status": {},
    "crazyflie": {
        "id_format": "{0:02}",
        "connections": ["crazyradio://0/80/2M/E7E7E7E7"],
        "debug": False,
        "enabled": False,
        "fence": {"enabled": True, "distance": 1, "action": "none"},
        "status_interval": 0.5,  # number of seconds between consecutive status reports from a drone
        "testing": False,
    },
    "debug": {
        "enabled": False,
        # "host": "localhost",
        # "port": 35424,
    },
    "dock": {"enabled": False, "listener": "unix:/tmp/skybrushd-dock.sock"},
    "flockctrl": {
        "id_format": "{0:02}",
        "connections": {
            "wireless": "default",
            # "wireless": "local",
            # "wireless": "192.168.1.0/24",
            "radio": "default",
        },
        "enabled": False,
    },
    "frontend": {},
    "gps": {
        "connection": "gpsd",
        "enabled": False,
        "format": "auto",
        "id_format": "GPS:{0}",
    },
    "hotplug": {},
    "http": {},
    "http_server": {},
    "insomnia": {"keep_display_on": False},
    "kp_index": {"source": "potsdam"},
    "license": {},
    "magnetic_field": {},
    "mavlink": {
        "enabled": False,
        "id_format": "{0:02}",
        "connections": [
            "default"
        ],  # default setup; listens for heartbeats on UDP port 14550, sends broadcasts to UDP port 14555
        "routing": {"rtk": 0},
        "system_id": 254,
    },
    "osc": {
        "enabled": False,
        "host": "localhost",
        "port": 10000,
        "interval": 0.5,  # number of seconds between consecutive status reports
        "path": "/skybrush",  # prefix to prepend to the OSC paths of the messages
    },
    "radiation": {
        "enabled": False,
        "sources": [{"lat": 47.473703, "lon": 19.061739, "intensity": 50000}],
        "background_intensity": 10,
    },
    "rc": {"enabled": False},
    "rc_udp": {"enabled": False},
    "rtk": {
        "presets": {
            # "javad": {
            #     "title": "JAVAD Triumph-2",
            #     "source": "tcp://192.168.47.1:8010",
            #     "format": "rtcm3",  # can be rtcm2, rtcm3 or auto
            # },
        },
        "add_serial_ports": True,
        # "gnss_types": "all",  # or a list like ["gps", "glonass"]
        "use_high_precision": True  # set to false if the rover cannot handle high-precision MSM RTK messages
        # "exclude_serial_ports": ["*ttyAMA*"
    },
    "sentry": {
        # Override the DSN to turn on Sentry integration
        "dsn": ""
    },
    "show": {
        "default_start_method": "rc"  # set to "auto" if you typically start shows automatically and not via a remote controller
    },
    "sidekick": {"enabled": False},
    "socketio": {},
    # "smpte_timecode": {"connection": "midi:IAC Driver Bus 1"},
    "ssdp": {},
    "system_clock": {},
    "tcp": {},
    "udp": {},
    "virtual_uavs": {
        "arm_after_boot": True,
        "add_noise": False,
        "count": 5,
        "delay": 0.2,
        "enabled": False,
        "id_format": "{0}",
        "origin": [18.915125, 47.486305, 215],  # Fahegy
        # "origin": [19.062159, 47.473360],  # ELTE kert
        "orientation": 59,
        "takeoff_area": {"type": "grid", "spacing": 5},
    },
    "weather": {},
    "webui": {
        "enabled": True,
        # "route": "/webui",
    },
}

# smpte_timecode seems to have some problems on a Mac - it consumes 15% CPU
# even when idle, and it starts throwing messages like this on the console
# after a while if there is no MIDI device:
#
# MidiInCore::initialize: error creating OS-X MIDI client object (-50)
if on_mac:
    EXTENSIONS.pop("smpte_timecode", None)
