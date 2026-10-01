# scripts/

Operator tools that run outside the panel.

- `build_demo_data_from_db.py`: builds the static extract the panel serves at `/demo/*` from the database. The
  demo seed (`python -m pim_studio.demo.seed`) runs it for you.
- `probe_locale.py`: asks the PIM for the same product under every locale, to see how it localises vocabulary.
- `probe_omnitron_write.py`: the one write that decides whether the panel may push at all. Run it once against
  the real PIM before `OMNITRON_WRITES_ENABLED` is ever set; it reports every field that moved besides the one
  it wrote.
- `storefront_reconcile.py`: reconciles the panel against the live storefronts, the only check that is not
  itself a report.

The probes and the reconcile talk to real systems and are not routed through demo mode.
