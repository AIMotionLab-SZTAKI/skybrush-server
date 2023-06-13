from .extension import LibmotioncaptureMocapExtension as construct

__all__ = ("construct",)

description = (
    "Connection to motion capture systems using a libmotioncapture abstraction layer"
)
dependencies = ("motion_capture",)
tags = ("experimental",)
schema = {
    "properties": {
        "connections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "title": "Connection type",
                        "enum": [
                            "vicon",
                            "optitrack",
                            "optitrack_closed_source",
                            "qualisys",
                            "nokov",
                            "vrpn",
                        ],
                        "default": "optitrack",
                    },
                    "hostname": {
                        "title": "Hostname",
                        "description": "Hostname of the mocap server to connect to",
                        "type": "string",
                        "default": "localhost",
                    },
                },
            },
        }
    }
}
