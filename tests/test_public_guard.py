"""The public-repo guard: no client, project or component names in the tracked tree.

The denylist and its checker live outside every repo, on the owner's machine, because
a committed list of the names being hidden would be the leak. Where they are absent
(CI, a fresh clone elsewhere) this test skips. Point `EMBARCH_ATLAS_PUBLIC_CHECK` at
another checker to use it instead.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = Path(os.environ.get("EMBARCH_ATLAS_PUBLIC_CHECK",
                              REPO.parent / ".atlas-local" / "tools" / "check_public.py"))


@pytest.mark.skipif(not CHECKER.is_file(), reason="no machine-local denylist here (CI or another machine)")
def test_tracked_tree_has_no_denylisted_names():
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode().split("\0")
    paths = [str(REPO / f) for f in tracked if f and (REPO / f).is_file()]
    r = subprocess.run([sys.executable, str(CHECKER), *paths], capture_output=True, text=True)
    # The checker prints file:line and a denylist entry NUMBER, never the name.
    assert r.returncode == 0, r.stdout[-3000:]
