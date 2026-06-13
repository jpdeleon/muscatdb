import sys
from pathlib import Path
import pytest

# Ensure prose2 is in python path
prose_path = "/ut2/jerome/github/research/project/ext_tools/prose2"
if prose_path not in sys.path:
    sys.path.insert(0, prose_path)

# Skip test if prose or twirl are not available
prose = pytest.importorskip("prose")
twirl = pytest.importorskip("twirl")

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
from prose import FITSImage, blocks
from prose.scripts.calibrate_muscat2 import _solve_wcs

def test_solve_wcs_muscat2_vs_muscat3():
    # 1. Load a real MuSCAT2 frame of TIC466376085
    m2_file = "/data/MuSCAT2/241001/MCT21_2410010091.fits"
    assert Path(m2_file).is_file(), f"MuSCAT2 test file {m2_file} not found"
    
    # 2. Get target coordinates from catalog/header
    # The header RA/Dec of this target is '21:48:31', '+6:42:55'
    target_coord = SkyCoord("21h48m31s", "+6d42m55s", frame="icrs")
    
    # 3. Create FITSImage and run detection
    img = FITSImage(m2_file)
    detection = blocks.detection.PointSourceDetection()
    detection.run(img)
    
    assert len(img.sources) >= 5, f"Too few sources detected: {len(img.sources)}"
    
    # 4. Solve WCS
    wcs_solved = _solve_wcs(img)
    assert wcs_solved is not None, "WCS solving failed on MuSCAT2 frame"
    
    # 5. Compare with MuSCAT3 correct header
    m3_file = "/data/MuSCAT3/250706/ogg2m001-ep02-20250706-0766-e91.fits"
    assert Path(m3_file).is_file(), f"MuSCAT3 test file {m3_file} not found"
    
    m3_hdr = fits.getheader(m3_file)
    m3_wcs = WCS(m3_hdr)
    
    # Assert WCS projection type (TAN) matches MuSCAT3 projection type
    assert wcs_solved.wcs.ctype[0] == m3_wcs.wcs.ctype[0] == "RA---TAN"
    assert wcs_solved.wcs.ctype[1] == m3_wcs.wcs.ctype[1] == "DEC--TAN"
    
    # Verify that the target coordinates map to a real detected source on the solved WCS within 1 arcminute
    min_dist = float("inf")
    for src in img.sources.coords:
        world = wcs_solved.pixel_to_world(src[0], src[1])
        dist = world.separation(target_coord).deg
        if dist < min_dist:
            min_dist = dist
            
    assert min_dist < 60.0 / 3600.0, f"Closest source distance {min_dist*3600:.2f} arcseconds is too large"
