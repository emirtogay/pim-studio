"""Demo mode: every external system, mocked from one synthetic catalogue.

Nothing here is imported unless `DEMO_MODE` is set — `pim_studio.integrations`
is the single switch, and it imports this package lazily. What lives here:

    catalog       the deterministic catalogue (invented products, real shapes)
    world         what every upstream holds on a given day, and the write journal
    pim           the PIM API, and `MockPim` — the real client on a mock transport
    library       `MockLibrary` (asset library) and `MockPhotoServer` (FTP folder)
    web           one transport for every demo host: PIM, shops, library, photos
    placeholder   the labelled colour-block JPEGs every demo image is
    seed          `python -m pim_studio.demo.seed` — fills a database for the demo
    history       the month of runs and sign-ins a seeded database is given
"""

from __future__ import annotations

from .library import MockLibrary, MockPhotoServer
from .pim import MockPim
from .web import transport
from .world import World, current, reset, use

__all__ = [
    "MockLibrary",
    "MockPhotoServer",
    "MockPim",
    "World",
    "current",
    "reset",
    "transport",
    "use",
]
