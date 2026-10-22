'''Per-proposal preparation script, to drop next to the proposal folder.

Pins the settings that belong to one beamtime -- the photon energy, the
output layout -- so a re-import is one call with no flags to remember. The
example is proposal 202601_PtAu (PXS at UDKM, U. Potsdam, 2026-01-08: Pt/Au
superlattice samples, mixed sRSM/tRSS/xRSS scans), written one .h5 per scan
under scans/.

Copy it into the proposal folder and edit the three constants.
'''
import argparse
import sys
from pathlib import Path

from pxs_import.prepare import prepare_proposal

PROPOSAL_ROOT = Path(__file__).resolve().parent
SINGLE_FILE = False
PHOTON_ENERGY = 8048.0

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--single-file', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        prepare_proposal(PROPOSAL_ROOT, single_file=SINGLE_FILE or args.single_file,
                          force=args.force, dry_run=args.dry_run,
                          photon_energy=PHOTON_ENERGY)
    except FileExistsError as exc:
        print(f'refusing to overwrite existing output ({exc}); use --force',
              file=sys.stderr)
        sys.exit(1)
