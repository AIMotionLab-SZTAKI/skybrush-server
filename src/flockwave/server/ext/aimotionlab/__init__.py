"""Template for Skybrush Server extensions.

This template is suitable for more complex extensions that may span multiple
files. ``__init__.py`` simply contains the metadata of the extension, and
imports the actual extension from another file. For simpler extensions, you
may put everything into a single file instead.

The entry point of the extension may be defined either by providing an
async ``run()`` function in this file, or by providing a synchronous
``construct()`` method that creates a new instance of the extension when
invoked with no arguments. The extension returned from the ``construct()``
method must be an object derived from ``flockwave.server.ext.base.Extension``.
When using the ``run()`` function, the entire module will be the extension;
when using an instance, the extension itself is the object returned from
``construct()``.  The former approach is preferred for simpler, mostly
stateless extensions (state can be stored in module-level variables). The
latter is preferred for more complex extensions as they can use the constructed
instance to store state.
"""

from .extension import aimotionlab as construct

__all__ = ("construct", )

description = "Template for Skybrush Server extensions"
"""The description of the extension that appears on the Skybrush server UI"""

dependencies = ("crazyflie", "signals", "show")
"""List of the names of other extensions that this extension depends on."""

tags = ("aimotionlab", "experimental")
"""List of tags that may be shown on the Skybrush server UI next to the
extension. This must be a list or tuple of strings, or a single space-separated
string.

Tags typically have no semantic meaning, but they are useful to organize
extensions into categories.
"""

schema = {
    "properties": {
        "channel": {
            "type": "integer",
            "default": 1,
            "description": (
                "The CRTP channel where the extension will send passive objects' poses."
            ),
        },
        "cf_port": {
            "type": "integer",
            "default": 1,
            "description": (
                "The CRTP port where the extension will send passive objects' poses."
            ),
        },
        "configWord": {
            "type": "string",
            "description": (
                "A configuration word, used for debugging and demonstrating the use of config schema."
            ),
        },
        "drone_port": {
            "type": "integer",
            "description": (
                "The TCP port number where the extension expects clients to connect, in order"
                "to send command to the drones from scripts."
            ),
        },
        "memory_partitions": {
            "type": "array",
            "description": (
                "The array of dictionaries describing the partitions of the crazyflie's trajectory memory."
                "A partition is defined by its ID number, its size, and the start of the partition."
                "Note that defining these partitions so they don't overlap is the responsibility of whoever"
                "is writing skybrushd.jsonc. The server will not double-check whether they overlap. It will"
                "only check whether a given trajectory fits into the trajectory partition specified by a given ID."
                "Dynamic partitions are the ones between which we swap when uploading trajectories dynamically."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "ID": {
                        "type": "integer"
                    },
                    "size": {
                        "type": "integer"
                    },
                    "start": {
                        "type": "integer"
                    },
                    "dynamic": {
                        "type": "boolean"
                    }
                }
            }

        }
    }
}
"""A JSON Schema description of the configuration object of the extension.
Refer to https://json-schema.org for more details, and take a look at a few
real Skybrush Server extensions for inspiration.

When omitted, the Skybrush server web UI will provide a JSON editor where you
can enter a JSON object directly. When provided, the schema will be used to
generate the configuration form of the extension in the Skybrush server web UI.

You may also provide a ``get_schema()`` function instead of the schema itself
if the schema has to be generated at runtime.
"""
