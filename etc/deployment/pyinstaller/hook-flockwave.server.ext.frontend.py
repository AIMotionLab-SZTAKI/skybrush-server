import flockwave.server.ext.frontend as frontend
import os

datas = [
    (
        os.path.join(os.path.dirname(frontend.__file__), "static"),
        "flockwave/server/ext/frontend/static",
    )
]
