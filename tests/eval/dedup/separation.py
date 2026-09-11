"""How far apart does SimHash actually put a scanned book and a different one?

``uv run poe simhash-separation``. Nothing here is a claim until it is measured, and the answer
decides what the near-duplicate check is allowed to *do* — see ``app/rag/simhash.py``.

Builds a passage, degrades it the way a scan does (character confusions, words split across a
column break, words lost entirely), and reports the distance against itself, against a
different printing, against a half-and-half document, and against unrelated text.

**What it showed** (seed 20260910, 64 bits): identical 0; scans at 2-10% word error 6-10; a
different printing 7; a different printing scanned at 5% error 16; a document that is half this
book and half another 16; an unrelated book 29.

The consequence is the one worth recording: there is **no gap** between "the same book, badly
scanned" and "half of a different book" — both sit at 16. A cut-off low enough to be safe
(around 10) misses a poor scan; one high enough to catch a poor scan also flags a document
sharing half its content. That is not a threshold to be tuned better, it is the limit of what
shingle overlap can tell you, and it is the measured reason this check suggests rather than
decides.

Simulated OCR is not real OCR, and one passage is not a corpus. Re-run this against real
scanned-versus-digital pairs before anything here is treated as a finding about production.
"""

import random

from app.rag.simhash import distance, simhash
from app.rag.textnorm import canonical

random.seed(20260910)

BOOK = (
    """
Photosynthesis is the process by which green plants and certain other organisms transform
light energy into chemical energy. During photosynthesis in green plants, light energy is
captured and used to convert water, carbon dioxide, and minerals into oxygen and energy-rich
organic compounds. It would be impossible to overestimate the importance of photosynthesis in
the maintenance of life on Earth. If photosynthesis ceased, there would soon be little food or
other organic matter on Earth. Most organisms would disappear, and in time Earth's atmosphere
would become nearly devoid of gaseous oxygen. The only organisms able to exist under such
conditions would be the chemosynthetic bacteria, which can utilize the chemical energy of
certain inorganic compounds and thus are not dependent on the conversion of light energy.
Energy produced by photosynthesis carried out by plants millions of years ago is responsible
for the fossil fuels that power industrial society. In past ages, green plants and small
organisms that fed on plants increased faster than they were consumed, and their remains were
deposited in Earth's crust by sedimentation and other geological processes.
"""
    * 3
)

OTHER_BOOK = (
    """
Mitosis is a process of cell duplication, or reproduction, during which one cell gives rise to
two genetically identical daughter cells. In a typical animal cell, mitosis can be divided into
four principal stages: prophase, metaphase, anaphase, and telophase. Before entering mitosis a
cell spends most of its life in interphase, during which the chromosomes are replicated. The
process of mitosis ensures that each daughter nucleus receives a complete and identical set of
chromosomes. Errors in this process can produce cells with too many or too few chromosomes, a
condition known as aneuploidy, which is a common feature of cancerous cells and of certain
developmental disorders in humans and other animals.
"""
    * 3
)

_CONFUSIONS = {"l": "1", "o": "0", "i": "1", "s": "5", "e": "c", "n": "m", "rn": "m", "g": "9"}


def ocr(text: str, error_rate: float) -> str:
    """Degrade text the way a scan does: character confusions, split and joined words."""
    out = []
    for word in text.split():
        if random.random() < error_rate:
            chars = list(word)
            i = random.randrange(len(chars))
            chars[i] = _CONFUSIONS.get(chars[i].lower(), chars[i])
            word = "".join(chars)
        if random.random() < error_rate / 3:
            continue  # a word the scan lost entirely
        if random.random() < error_rate / 3 and len(word) > 4:
            word = word[:2] + " " + word[2:]  # a word split across a column break
        out.append(word)
    return " ".join(out)


def edition(text: str) -> str:
    """A different printing: front matter, page furniture, a reworded sentence."""
    return (
        "CHAPTER 4  PHOTOSYNTHESIS  |  87\n\n"
        + text.replace(
            "It would be impossible to overestimate the importance",
            "The importance cannot be overstated",
        )
        + "\n\nReview questions 4.1-4.8   Further reading   Index terms\n"
    )


base = simhash(canonical(BOOK))
print(f"{'comparison':<44} {'distance':>8}  {'bits agreeing':>13}")
print("-" * 70)
for label, other in [
    ("itself", BOOK),
    ("scan, 2% word error", ocr(BOOK, 0.02)),
    ("scan, 5% word error", ocr(BOOK, 0.05)),
    ("scan, 10% word error", ocr(BOOK, 0.10)),
    ("scan, 20% word error", ocr(BOOK, 0.20)),
    ("a different printing", edition(BOOK)),
    ("different printing, scanned at 5%", ocr(edition(BOOK), 0.05)),
    ("half this book, half another", BOOK[: len(BOOK) // 2] + OTHER_BOOK[: len(OTHER_BOOK) // 2]),
    ("an unrelated book", OTHER_BOOK),
]:
    d = distance(base, simhash(canonical(other)))
    print(f"{label:<44} {d:>8}  {(64 - d) / 64:>12.0%}")
