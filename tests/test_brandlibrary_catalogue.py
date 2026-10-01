"""Matching library assets to options.

The interesting failures here are all silent: a wrong match publishes another
colour's photo onto a product page, and a name collision overwrites a file that
was uploaded correctly. Neither raises anything, so both are asserted.
"""

from __future__ import annotations

from pim_studio.brandlibrary.catalogue import (
    brandlibrary_key,
    candidate_keys,
    dedupe,
    is_product_photo,
    match,
    remote_name,
)
from pim_studio.brandlibrary.client import ZALANDO_KEY, Asset


def asset(name: str, *, zalando: bool = False, ident: str = "", **kw) -> Asset:
    """A library asset with no metaproperties — so these exercise the filename
    fallback, which is the path that has to survive a naming change."""
    return Asset(
        id=ident or name,
        name=name,
        extension=kw.pop("extension", "jpg"),
        thumbnails={ZALANDO_KEY: f"https://cdn/{name}_zal.jpg"} if zalando else {},
        **kw,
    )


# ── candidate keys ─────────────────────────────────────────────────────────


def test_the_colour_is_offered_before_the_style() -> None:
    """Most specific first, so a base code cannot claim every colour's photos."""
    keys = candidate_keys("AF0276_J2G_32")

    assert keys.index("AF0276J2G") < keys.index("AF0276")
    # And the whole name — the strongest evidence there is — leads.
    assert keys[0] == "AF0276J2G32"


def test_every_separator_spelling_produces_the_same_keys() -> None:
    """Five conventions describe one ModelColor; they must not match differently."""
    expected = candidate_keys("AF0276_J2G_32")
    for spelling in ("AF0276-J2G-32", "AF0276.J2G.32", "AF0276 J2G 32", "AF0276/J2G/32"):
        assert candidate_keys(spelling) == expected


# ── matching ───────────────────────────────────────────────────────────────


def test_assets_group_under_the_option_they_belong_to() -> None:
    assets = [asset("AF0276_J2G_32"), asset("AF0276_J2G_31"), asset("L3558_132_01")]

    found = match(assets, ["AF0276.J2G", "L3558.132"])

    assert sorted(a.name for a in found["AF0276.J2G"]) == ["AF0276_J2G_31", "AF0276_J2G_32"]
    assert [a.name for a in found["L3558.132"]] == ["L3558_132_01"]


def test_a_shorter_option_does_not_swallow_a_longer_one() -> None:
    """The reason matching is on parts and not on a raw string prefix.

    `squash("L3558.13")` is a prefix of `squash("L3558_132_01")`, so a
    startswith test would file colour 132's photo under colour 13 — a wrong
    photo on a real product page, with nothing raised anywhere.
    """
    found = match([asset("L3558_132_01")], ["L3558.13", "L3558.132"])

    assert "L3558.13" not in found
    assert [a.name for a in found["L3558.132"]] == ["L3558_132_01"]


def test_an_unknown_name_is_left_out_rather_than_invented() -> None:
    """A token matching nothing is reported as unmatched, never turned into a
    plausible ModelColor no product has."""
    assert match([asset("XX9999_ZZZ_01")], ["AF0276.J2G"]) == {}


def test_matching_is_case_and_spelling_insensitive() -> None:
    found = match([asset("af0276-j2g-32")], ["AF0276.J2G"])

    assert [a.name for a in found["AF0276.J2G"]] == ["af0276-j2g-32"]


def test_a_shoot_prefix_does_not_hide_the_code() -> None:
    """349 of the 1,101 assets the first run published look like this.

    `3K_` and `WHITE_` are shoot and colourway prefixes sitting ahead of the
    style code. Matching only leading runs found 752 of 1,101 and dropped the
    rest — and a dropped asset does not look like a bug, it looks like an
    option the library does not hold, which is a question somebody then takes
    to the brand.
    """
    found = match(
        [asset("3K_AF8242_ADY_37"), asset("WHITE_L3558_132_01")],
        ["AF8242.ADY", "L3558.132"],
    )

    assert [a.name for a in found["AF8242.ADY"]] == ["3K_AF8242_ADY_37"]
    assert [a.name for a in found["L3558.132"]] == ["WHITE_L3558_132_01"]


def test_a_longer_window_beats_a_shorter_one_elsewhere_in_the_name() -> None:
    """Ordering is the safety argument for allowing windows at all.

    With a three-part option in play, the three-part window must win over any
    two-part window later in the same name.
    """
    found = match([asset("3K_60KFA0791_83J_L1")], ["60KFA0791.83J", "83J.L1"])

    assert [a.name for a in found["60KFA0791.83J"]] == ["3K_60KFA0791_83J_L1"]
    assert "83J.L1" not in found


def test_a_shoe_matches_the_code_the_library_holds() -> None:
    """No footwear has been through this pipeline yet, so this is the test
    standing in for the first shoe run.

    Omnitron writes `774RMA3517.255`; the library holds `74RMA3517.255`. Get
    this wrong and the shoe is not an error — it is recorded as a photo the
    brand has never taken.
    """
    found = match(
        [asset("74RMA3517_255_01"), asset("3K_14KMA1408_02H_20")],
        ["774RMA3517.255", "714KMA1408.02H"],
    )

    assert [a.name for a in found["774RMA3517.255"]] == ["74RMA3517_255_01"]
    assert [a.name for a in found["714KMA1408.02H"]] == ["3K_14KMA1408_02H_20"]


def test_a_shoe_matches_on_metadata_too() -> None:
    """The library states the option in `property_BI_*`, in its own spelling.

    Metadata is the primary match, so the alias has to reach it as well — a
    rule that only works on filenames would fail every shoe whose asset is
    named anything else.
    """
    shoe = Asset(
        id="1",
        name="anything_at_all",
        extension="jpg",
        product_id="74RMA3517",
        color_id="255",
    )

    found = match([shoe], ["774RMA3517.255"])

    assert [a.id for a in found["774RMA3517.255"]] == ["1"]


def test_a_numeric_code_starting_with_seven_is_not_treated_as_a_shoe() -> None:
    """`7006103.433` is a real code and is not footwear. Stripping its 7 would
    make a product that has photos appear not to."""
    found = match([asset("7006103_433_01")], ["7006103.433"])

    assert [a.name for a in found["7006103.433"]] == ["7006103_433_01"]


def test_a_watch_or_accessory_code_still_matches() -> None:
    """`2011169.-` — the identifier shape that broke the old size-suffix rule."""
    found = match([asset("2011169_-_01")], ["2011169.-"])

    assert [a.name for a in found["2011169.-"]] == ["2011169_-_01"]


def test_swatches_are_not_product_photography() -> None:
    """A colour chip published as a product photo reads as a broken image."""
    assert not is_product_photo(asset("swatch_AF0276_J2G"))
    assert is_product_photo(asset("AF0276_J2G_32"))
    assert match([asset("swatch_AF0276_J2G")], ["AF0276.J2G"]) == {}


def test_an_asset_with_no_name_is_skipped() -> None:
    assert match([Asset(id="1", name="")], ["AF0276.J2G"]) == {}


# ── the collision that cost 228 uploads ────────────────────────────────────


def test_one_asset_survives_per_name() -> None:
    kept = dedupe([asset("AF0276_J2G_32", ident="a"), asset("AF0276_J2G_32", ident="b")])

    assert len(kept) == 1


def test_the_zalando_carrying_record_is_the_one_kept() -> None:
    """Not an arbitrary tie-break: it is what makes one upload serve both
    outputs. The other record can only produce the original."""
    kept = dedupe(
        [
            asset("AF0276_J2G_32", ident="plain"),
            asset("AF0276_J2G_32", ident="withzal", zalando=True),
        ]
    )

    assert [a.id for a in kept] == ["withzal"]


def test_the_zalando_record_wins_from_either_direction() -> None:
    """Order of arrival is not something the library promises."""
    kept = dedupe(
        [
            asset("AF0276_J2G_32", ident="withzal", zalando=True),
            asset("AF0276_J2G_32", ident="plain"),
        ]
    )

    assert [a.id for a in kept] == ["withzal"]


def test_distinct_names_are_all_kept() -> None:
    kept = dedupe([asset("AF0276_J2G_32"), asset("AF0276_J2G_31")])

    assert len(kept) == 2


# ── naming on the photo server ─────────────────────────────────────────────


def test_the_original_keeps_the_asset_name() -> None:
    assert remote_name(asset("AF0276_J2G_32"), "original") == "AF0276_J2G_32.jpg"


def test_the_zalando_suffix_matches_what_is_already_published() -> None:
    """`_ZAL`, because 1,603 files are already on the server under it.

    The resume check compares filenames against the remote listing, so a
    tidier suffix would re-upload every one of them and orphan what is there.
    """
    assert remote_name(asset("AF0276_J2G_32"), "zalando") == "AF0276_J2G_32_ZAL.jpg"


def test_the_library_spelling_of_a_modelcolor() -> None:
    assert brandlibrary_key("AF0276.J2G") == "AF0276_J2G"
