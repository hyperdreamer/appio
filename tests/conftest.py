"""Make subprocess-based tests hermetic.

Several tests spawn ``sys.executable -c ...`` children that import :mod:`appio`
(``tests/p3/test_iod.py`` crash recovery, and others). A child inherits the
parent's environment but *not* pytest's ``pythonpath`` setting, so without this
it imports whatever ``appio`` happens to be installed system-wide — on a
developer host that is a stale release, and on a clean machine the import fails
outright with ``ModuleNotFoundError``.

Running the suite on a second host exposed it: the crash-recovery test passed
here only because a previous release was installed in site-packages, and failed
on the machine where nothing was installed.

Point children at the tree under test so what they exercise is what this suite
is running.
"""

import os
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

_existing = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = str(SRC) if not _existing else f"{SRC}{os.pathsep}{_existing}"
