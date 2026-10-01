"""BrandLibrary — finding photos for options that have none, and handing them on.

Products reach the four sites without photos. Often the photo exists: in the
brand's asset library, which the sites cannot see. This package finds those
options, publishes the assets to our own folder on the photo server, and builds
a bundle for the Turkey team to upload — because our Omnitron connection is
read-only by decision, so we can prepare an image but never write one.

Four steps, each its own module and each separately runnable, because the
expensive one is in the middle and re-running the whole thing to retry the end
is how the original script kept losing an afternoon:

    targets   who has no photo            a database read, no network
    client    the brand library           Bynder, read-only
    publish   photo server + manifest     FTP, resumable
    bundle    ZIP + Excel for Turkey      the actual deliverable

Not a `sync/` stage. Every stage there takes an `OmnitronClient` and answers to
Omnitron's watermark; this reads a different upstream on a different cadence.
It does reuse `sync.runs`, so a run lands on the same `Sync Runs` page.
"""
