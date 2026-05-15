"""Default organism list for Growlithe.

The default is a manageable subset (9 organisms) covering the WHO
priority foodborne and AMR-sentinel pathogens that NCBI Pathogen
Detection tracks. The operator overrides the list via
``KANTO_GROWLITHE_ORGANISMS`` (comma-separated). The names must match
the NCBI URL path segments under ``/pathogen/Results/`` exactly —
NCBI is case-sensitive and uses ``_`` rather than space for
multi-word names.

References:
  * NCBI Pathogen Detection coverage:
    https://www.ncbi.nlm.nih.gov/pathogens/about/
  * WHO priority pathogens list:
    https://www.who.int/publications/i/item/9789240093461
"""

from __future__ import annotations

DEFAULT_ORGANISMS: tuple[str, ...] = (
    "Salmonella",
    "Campylobacter_jejuni",
    "E.coli_Shigella",
    "Listeria",
    "Klebsiella_pneumoniae",
    "Acinetobacter",
    "Staphylococcus_aureus",
    "Enterococcus_faecium",
    "Pseudomonas_aeruginosa",
)

__all__ = ["DEFAULT_ORGANISMS"]
