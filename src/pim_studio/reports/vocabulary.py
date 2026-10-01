"""Is the filter vocabulary fit to put in front of a customer?

The premise behind the filter work was that these attributes are populated and
would help people navigate. Populated they are. But collar currently offers 26
options of which six are duplicates and four belong to other attributes, all
labelled in Turkish. Filling the gaps on a vocabulary in this state adds
options to a filter that already needs pruning, and this report exists to say
so with numbers rather than impressions.

Scoped by default to the seven garment attributes the filter goal rests on.
Widening it to all 149 is possible and mostly noise: sizes and colours share
labels across attributes for legitimate reasons, and a contamination check over
20,382 values reports hundreds of collisions nobody will act on.

## What it looks for, and why each rule is shaped the way it is

**Redundant labels.** Two values that render as the same text are two ways to
pick the same thing. Compared casefolded and trimmed, because the duplicates in
this tenant are mostly casing (`BOĞAZLI YAKA` against `Boğazlı Yaka`).

**Cross-attribute contamination.** A label appearing under more than one
attribute. `STANDARD FIT` sits in the collar list; `Regular` is in both waist
and length. Some collisions are legitimate — `Denim` is honestly both a pattern
and a fabric — so this reports rather than accuses.

**Unlabelled codes.** A value whose label is just the code repeated, in an
attribute that otherwise carries real labels. `desen` value `000` has label
`000` and appears on 165 rows of the polo export alone: present, non-null and
meaningless. This is the "filled and defective" class that a not-null check
launders into "fine".

**Placeholders.** A `-` offered as a filter option. It means "none" and reads
as a broken row.

A rule that was written and then deleted: "an attribute that mixes code-keyed
and text-keyed values". Measured against the real data, no attribute does —
`desen` is entirely code-keyed and the other six entirely text-keyed. Six
healthy `yaka` values had tripped an earlier version of the check because their
label equals their value, which is correct for a text-keyed attribute. A rule
that fires on healthy data trains people to ignore the report.

## What it cannot tell you

Per-market fill — how many products in each market actually carry a value.
That needs product attribute values, which are not imported yet. Reported as
unavailable rather than approximated, because a plausible-looking number here
would be acted on.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from ..db import session_scope
from ..models import OmnitronAttribute, OmnitronAttributeValue

# The seven the filter goal rests on. Imported from the reconciliation report
# rather than restated, so the two cannot drift apart.
from .registry import FILTER_ATTRIBUTES

#: A value that is a bare numeric code. `desen` is keyed entirely this way.
_CODE = re.compile(r"^\d+$")

#: Values that mean "nothing" but occupy a slot in a dropdown.
_PLACEHOLDERS = {"-", "--", "", "none", "n/a"}


def _norm(text: str | None) -> str:
    """Casefold for matching, with the Turkish dotless-ı problem handled.

    Python's `casefold` uses the default Unicode mapping: `I` becomes dotted
    `i`, while dotless `ı` stays `ı`. So `BOĞAZLI` and `Boğazlı` — the same
    word, entered twice, in the tenant's own language — fold to different
    strings and read as two distinct labels. Mapping ı to i (and stripping the
    combining dot that casefolding `İ` leaves behind, U+0307) makes the two
    meet. For matching only; nothing displayed goes through this.
    """
    folded = (text or "").strip().casefold()
    return folded.replace("ı", "i").replace("̇", "")


@dataclass
class RedundantLabel:
    attribute: str
    label: str
    values: list[str]


@dataclass
class Contamination:
    label: str
    attributes: list[str]


@dataclass
class DefectiveValue:
    attribute: str
    value: str
    label: str
    reason: str


@dataclass
class AttributeSummary:
    key: str
    gloss: str
    values: int
    distinct_labels: int

    @property
    def redundant(self) -> int:
        return self.values - self.distinct_labels


@dataclass
class VocabularyReport:
    summaries: list[AttributeSummary] = field(default_factory=list)
    redundant: list[RedundantLabel] = field(default_factory=list)
    contamination: list[Contamination] = field(default_factory=list)
    defective: list[DefectiveValue] = field(default_factory=list)
    missing_attributes: list[str] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "attributes": len(self.summaries),
            "values": sum(s.values for s in self.summaries),
            "distinct_labels": sum(s.distinct_labels for s in self.summaries),
            "redundant": sum(s.redundant for s in self.summaries),
            "contaminated_labels": len(self.contamination),
            "defective_values": len(self.defective),
        }


def audit_vocabulary(keys: dict[str, str] | None = None) -> VocabularyReport:
    """Audit the mirrored vocabulary. Reads only.

    `keys` maps attribute key to a short gloss; defaults to the seven filter
    attributes. Pass a wider mapping to audit more, accepting the noise.
    """
    wanted = keys if keys is not None else FILTER_ATTRIBUTES
    report = VocabularyReport()

    with session_scope() as db:
        attributes = {
            a.key: a
            for a in db.scalars(
                select(OmnitronAttribute).where(OmnitronAttribute.key.in_(list(wanted)))
            )
        }
        for key in wanted:
            if key not in attributes:
                report.missing_attributes.append(key)

        if not attributes:
            return report

        values_by_attribute: dict[str, list[OmnitronAttributeValue]] = defaultdict(list)
        pk_to_key = {a.pk: a.key for a in attributes.values()}
        for option in db.scalars(
            select(OmnitronAttributeValue).where(
                OmnitronAttributeValue.attribute.in_(list(pk_to_key))
            )
        ):
            values_by_attribute[pk_to_key[option.attribute]].append(option)

        label_owners: dict[str, set[str]] = defaultdict(set)

        for key in wanted:
            if key not in attributes:
                continue
            rows = values_by_attribute.get(key, [])
            by_label: dict[str, list[str]] = defaultdict(list)
            for row in rows:
                label = _norm(row.label)
                if label:
                    by_label[label].append(row.value or "")
                    label_owners[label].add(key)

            report.summaries.append(
                AttributeSummary(
                    key=key,
                    gloss=wanted[key],
                    values=len(rows),
                    distinct_labels=len(by_label),
                )
            )

            for label, values in sorted(by_label.items()):
                if len(values) > 1:
                    report.redundant.append(
                        RedundantLabel(attribute=key, label=label, values=sorted(values))
                    )

            # An attribute "uses labels" when most of its rows carry a label
            # different from the value. Only then does label == value mean the
            # label is missing rather than the attribute being text-keyed.
            labelled = sum(1 for r in rows if _norm(r.label) != _norm(r.value))
            uses_labels = bool(rows) and labelled / len(rows) >= 0.5

            for row in rows:
                value, label = (row.value or "").strip(), (row.label or "").strip()
                if _norm(value) in _PLACEHOLDERS or _norm(label) in _PLACEHOLDERS:
                    report.defective.append(
                        DefectiveValue(key, value, label, "placeholder offered as an option")
                    )
                elif not label:
                    report.defective.append(
                        DefectiveValue(key, value, label, "no label at all")
                    )
                elif uses_labels and _CODE.match(value) and _norm(label) == _norm(value):
                    report.defective.append(
                        DefectiveValue(key, value, label, "code repeated as its own label")
                    )

        for label, owners in sorted(label_owners.items()):
            if len(owners) > 1:
                report.contamination.append(
                    Contamination(label=label, attributes=sorted(owners))
                )

    return report
