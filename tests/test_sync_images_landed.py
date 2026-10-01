"""The image sync closes the loop on photos we published.

`published_images.landed_at` is the exported -> live hop: set when Omnitron
shows an image for an option we handed to Turkey. Until 0.46.0 nothing wrote
it, so "published, with Turkey" only ever grew — 149 on a day when 86 of those
options already had a photo on the site.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import Attribute, Product, PublishedImage
from pim_studio.sync import liveness

pytestmark = pytest.mark.usefixtures("temp_db")


class FakeApi:
    """Answers the product read with a first image per pk, or none."""

    def __init__(self, images: dict[int, str]) -> None:
        self.images = images

    def iter_records(self, path: str, params=None, page_size: int = 100):  # noqa: ARG002
        assert path == "/api/v1/products/", path
        for pk in sorted(int(p) for p in params["pk__in"].split(",")):
            url = self.images.get(pk)
            yield {"pk": pk, "productimage_set": [{"image": url}] if url else []}


class FakeRun:
    id = 1

    def seen(self, n: int = 1) -> None:
        pass

    def imported(self, n: int = 1) -> None:
        pass


def _published_options(*pairs: tuple[int, str]) -> None:
    with session_scope() as db:
        db.add(
            Attribute(
                key="image_0",
                label="image_0",
                data_type="text",
                write_channel="read_only",
                value_grain="model_color",
            )
        )
        for pk, model_color in pairs:
            db.add(
                Product(
                    sku=f"{model_color}.T04",
                    base_code_format=f"{model_color}.T04",
                    model_color=model_color,
                    base_code=model_color.split(".")[0],
                    omnitron_pk=pk,
                    omnitron_product_type=0,
                )
            )
            db.add(
                PublishedImage(
                    model_color=model_color,
                    brandlibrary_key=model_color.replace(".", "_"),
                    asset_name=f"{model_color}_01",
                    edition="original",
                    remote_name=f"{model_color}_01.jpg",
                    public_url=f"https://photos.example/pim-studio/{model_color}_01.jpg",
                    bytes=1,
                )
            )


def _landed() -> dict[str, bool]:
    with session_scope() as db:
        return {
            row.model_color: row.landed_at is not None
            for row in db.scalars(select(PublishedImage))
        }


def test_a_published_option_omnitron_now_shows_has_landed() -> None:
    _published_options((101, "AF0276.J2G"), (102, "5H3389.CCS"))

    result = liveness.sync_images(FakeApi({101: "https://cdn/af.jpg"}), FakeRun())

    assert result["landed"] == 1
    assert _landed() == {"AF0276.J2G": True, "5H3389.CCS": False}


def test_landing_is_recorded_once() -> None:
    """The first sync that sees it is the landing date; later ones leave it."""
    _published_options((101, "AF0276.J2G"))
    api = FakeApi({101: "https://cdn/af.jpg"})

    liveness.sync_images(api, FakeRun())
    again = liveness.sync_images(api, FakeRun())

    assert again["landed"] == 0
    assert _landed() == {"AF0276.J2G": True}
