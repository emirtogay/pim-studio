# Changelog

What changed in PIM Studio, newest first. The What's New page in the panel is
built from this file.

## 0.48.7 — 2026-09-30

**Demo mode.** Set `DEMO_MODE=1` and every external system — the PIM API, the
asset library, the photo server and the four storefronts — is replaced by a
mock fed from one synthetic catalogue. Nothing leaves the process.

- `python -m pim_studio.demo.seed` fills an empty database by running the real
  sync stages against the mocks, then a photo publish, a push and a takedown.
- The panel's own pages work unchanged on top of it, including the buttons
  that write.

## 0.47.0 — 2026-09-17

**Descriptions come from the key each shop renders.** The market-suffixed
description field is now the live value; the shared multi-language field is
only offered as a proposal where the shop's own text is missing or in the
wrong language.

- A description the rendered key no longer holds is cleared, with the old text
  kept in the product's history.

## 0.46.0 — 2026-09-10

**Published photos are marked as landed.** When the PIM starts showing an image
for an option we published a photo for, the photo is recorded as landed, so
the "waiting for upload" count only holds what is genuinely outstanding.

## 0.44.0 — 2026-09-07

**Observed liveness.** A new `storefront` stage reads each market's public
listing and records which options are actually on sale. Where it has a
reading, the panel shows what the shop sells instead of what the rules predict,
and lists the disagreements as their own queue.

- Options that leave a listing are marked with the time they left, not deleted.

## 0.40.0 — 2026-09-03

**Take a product offline, and put it back.** Product pages can remove an option
from every catalogue in a market group after a typed confirmation. Each removed
catalogue row is recorded first, so a restore puts back exactly what was taken.

## 0.35.0 — 2026-08-28

**Push approved values to the PIM.** Values set in the panel, accepted from the
proposal queue or decided on the conflicts page can be pushed from the Imports
page.

- Every size is read back after the write; anything else that moved halts the
  batch.
- A `publication` stage then asks each shop whether it serves the new text.

## 0.24.1 — 2026-08-22

Release notes and data extracts are always revalidated by the browser, so the
version in the sidebar and the one on this page can no longer disagree.

## 0.20.0 — 2026-08-18

**The conflicts queue.** When the sizes of one option disagree about a value,
the importer settles cosmetic differences itself and queues the rest with every
candidate and how many sizes reported it, for a person to decide.
