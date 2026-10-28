"""Validate the distributed PDFs, including real raster marker recognition.

These tests read the finished files; they never call the PDF generator. Digital
dimensions do not certify a printer's physical scaling, which must be measured.
"""
import json
import math
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRINT = ROOT/"output/pdf/microduck-aruco-print-pack.pdf"
GUIDE = ROOT/"output/pdf/microduck-aruco-placement-guide.pdf"
POINTS_PER_MM = 72/25.4
DPI = 150
PIXELS_PER_MM = DPI/25.4


def reader(path):
    pypdf = pytest.importorskip("pypdf")
    assert path.is_file(), f"Distributed print asset is missing: {path}"
    return pypdf.PdfReader(path)


def multiply(left, right):
    a,b,c,d,e,f = left
    g,h,i,j,k,l = right
    return (a*g+c*h, b*g+d*h, a*i+c*j, b*i+d*j, a*k+c*l+e, b*k+d*l+f)


def filled_black_rectangles(page, document):
    """Read filled vector rectangles, respecting PDF graphics transforms/state."""
    from pypdf.generic import ContentStream
    transform = (1,0,0,1,0,0)
    black = True
    stack, pending, rectangles = [], [], []
    for operands, operator in ContentStream(page.get_contents(), document).operations:
        if operator == b"q":
            stack.append((transform, black))
        elif operator == b"Q":
            transform, black = stack.pop()
        elif operator == b"cm":
            transform = multiply(transform, tuple(float(v) for v in operands))
        elif operator == b"rg":
            black = all(abs(float(v)) < 1e-8 for v in operands)
        elif operator == b"g":
            black = abs(float(operands[0])) < 1e-8
        elif operator == b"k":
            black = abs(float(operands[3])-1) < 1e-8
        elif operator == b"re":
            x,y,width,height = (float(v) for v in operands)
            a,b,c,d,e,f = transform
            corners = [(a*px+c*py+e,b*px+d*py+f)
                       for px,py in ((x,y),(x+width,y),(x+width,y+height),(x,y+height))]
            pending.append((min(p[0] for p in corners),min(p[1] for p in corners),
                            max(p[0] for p in corners),max(p[1] for p in corners)))
        elif operator in {b"f",b"f*",b"B",b"B*",b"b",b"b*"}:
            if black:
                rectangles.extend(pending)
            pending = []
        elif operator in {b"n",b"S",b"s"}:
            pending = []
    assert not stack
    return [tuple(value/POINTS_PER_MM for value in rectangle) for rectangle in rectangles]


@pytest.mark.parametrize("path",[PRINT,GUIDE],ids=["print-pack","placement-guide"])
def test_distributed_documents_are_five_unrotated_a4_pages(path):
    document = reader(path)
    assert len(document.pages) == 5
    for page in document.pages:
        assert float(page.mediabox.width)/POINTS_PER_MM == pytest.approx(210,abs=.001)
        assert float(page.mediabox.height)/POINTS_PER_MM == pytest.approx(297,abs=.001)
        assert [float(x) for x in page.cropbox] == pytest.approx([float(x) for x in page.mediabox],abs=.001)
        assert int(page.get("/Rotate",0)) % 360 == 0
        assert float(page.get("/UserUnit",1)) == 1
    if path == PRINT:
        assert document.trailer["/Root"]["/ViewerPreferences"]["/PrintScaling"] == "/None"


@pytest.mark.parametrize("page_index",range(4),ids=["id-0","id-1","id-2","id-3"])
def test_printed_marker_vector_boundaries_are_exactly_160mm(page_index):
    document = reader(PRINT)
    rectangles = filled_black_rectangles(document.pages[page_index],document)
    assert rectangles
    left, bottom = min(r[0] for r in rectangles), min(r[1] for r in rectangles)
    right, top = max(r[2] for r in rectangles), max(r[3] for r in rectangles)
    assert [left,bottom,right,top] == pytest.approx([25,80,185,240],abs=.001)
    assert right-left == pytest.approx(160,abs=.001)
    assert top-bottom == pytest.approx(160,abs=.001)
    # 4 encoded bits + one black border module on each side.
    for x0,y0,x1,y1 in rectangles:
        assert x1-x0 == pytest.approx(160/6,abs=.001)
        assert y1-y0 == pytest.approx(160/6,abs=.001)
        assert (x0-left)/(160/6) == pytest.approx(round((x0-left)/(160/6)),abs=.001)
        assert (y0-bottom)/(160/6) == pytest.approx(round((y0-bottom)/(160/6)),abs=.001)
    manifest = json.loads((ROOT/"config/aruco-landmarks.json").read_text())
    assert manifest["print"]["black_square_mm"] == 160
    assert manifest["markers"][str(page_index)]["size_m"]*1000 == pytest.approx(right-left,abs=.001)
    assert (bottom+top)/2 == pytest.approx(manifest["print"]["marker_center_from_paper_bottom_mm"],abs=.001)
    # A 20 mm exterior white region fits on all four sides; the raster test
    # below checks that it is actually blank rather than only available space.
    assert left >= 20 and 210-right >= 20 and bottom >= 20 and 297-top >= 20


def test_calibration_board_has_exact_20mm_vector_squares():
    document = reader(PRINT)
    rectangles = filled_black_rectangles(document.pages[4],document)
    assert len(rectangles) == 27  # Half of the 9 x 6 black/white cells.
    found = set()
    for x0,y0,x1,y1 in rectangles:
        assert x1-x0 == pytest.approx(20,abs=.001)
        assert y1-y0 == pytest.approx(20,abs=.001)
        col, row = round((x0-15)/20), round((220-y1)/20)
        assert x0 == pytest.approx(15+col*20,abs=.001)
        assert y1 == pytest.approx(220-row*20,abs=.001)
        found.add((row,col))
    assert found == {(row,col) for row in range(6) for col in range(9) if (row+col)%2 == 0}
    assert max(r[2] for r in rectangles)-min(r[0] for r in rectangles) == pytest.approx(180,abs=.001)
    assert max(r[3] for r in rectangles)-min(r[1] for r in rectangles) == pytest.approx(120,abs=.001)


@pytest.fixture(scope="module")
def rendered_print_pages(tmp_path_factory):
    executable = shutil.which("pdftoppm")
    if not executable:
        pytest.skip("pdftoppm is required for independent PDF raster verification")
    cv2 = pytest.importorskip("cv2")
    prefix = tmp_path_factory.mktemp("aruco-pdf-raster")/"print"
    subprocess.run([executable,"-r",str(DPI),"-gray","-png",str(PRINT),str(prefix)],
                   check=True,capture_output=True,timeout=60)
    pages = sorted(prefix.parent.glob("print-*.png"))
    assert len(pages) == 5
    images = [cv2.imread(str(path),cv2.IMREAD_GRAYSCALE) for path in pages]
    assert all(image is not None for image in images)
    return images


@pytest.mark.parametrize("identifier",range(4),ids=["id-0","id-1","id-2","id-3"])
def test_raster_marker_id_corners_size_and_twenty_mm_quiet_zone(rendered_print_pages,identifier):
    cv2, np = pytest.importorskip("cv2"),pytest.importorskip("numpy")
    image = rendered_print_pages[identifier]
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    corners, ids, _ = cv2.aruco.ArucoDetector(dictionary,parameters).detectMarkers(image)
    assert ids is not None and ids.flatten().tolist() == [identifier]
    expected = np.array([[25,57],[185,57],[185,217],[25,217]],float)*PIXELS_PER_MM
    observed = corners[0].reshape(4,2)
    assert observed == pytest.approx(expected,abs=2)
    for i in range(4):
        length_mm = float(np.linalg.norm(observed[(i+1)%4]-observed[i]))/PIXELS_PER_MM
        assert length_mm == pytest.approx(160,abs=.4)  # <=2.4 pixels at 150 dpi.
    # Blank 20 mm ring x5..205, y60..260 in bottom-origin PDF coordinates.
    # Exclude only a two-pixel rasterisation fringe touching the black edge.
    outer_left,outer_right = math.ceil(5*PIXELS_PER_MM),math.floor(205*PIXELS_PER_MM)
    outer_top,outer_bottom = math.ceil(37*PIXELS_PER_MM),math.floor(237*PIXELS_PER_MM)
    left,right = math.floor(25*PIXELS_PER_MM)-2,math.ceil(185*PIXELS_PER_MM)+2
    top,bottom = math.floor(57*PIXELS_PER_MM)-2,math.ceil(217*PIXELS_PER_MM)+2
    strips = [image[outer_top:top,outer_left:outer_right],
              image[bottom:outer_bottom,outer_left:outer_right],
              image[outer_top:outer_bottom,outer_left:left],
              image[outer_top:outer_bottom,right:outer_right]]
    assert all(strip.size and int(strip.min()) >= 254 for strip in strips)


def test_raster_checkerboard_has_eight_by_five_inner_corners_at_twenty_mm(rendered_print_pages):
    cv2, np = pytest.importorskip("cv2"),pytest.importorskip("numpy")
    found, corners = cv2.findChessboardCornersSB(rendered_print_pages[4],(8,5),
                                                 flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
    assert found
    observed = corners.reshape(5,8,2)
    horizontal = np.linalg.norm(np.diff(observed,axis=1),axis=2)/PIXELS_PER_MM
    vertical = np.linalg.norm(np.diff(observed,axis=0),axis=2)/PIXELS_PER_MM
    assert horizontal == pytest.approx(np.full((5,7),20),abs=.2)
    assert vertical == pytest.approx(np.full((4,8),20),abs=.2)
    # The board spans x15..195 / y100..220 mm. Internal corner bounds are
    # x35..175 / y120..200; image top-origin therefore y97..177 mm.
    flat = observed.reshape(-1,2)/PIXELS_PER_MM
    assert flat.min(axis=0) == pytest.approx([35,97],abs=.2)
    assert flat.max(axis=0) == pytest.approx([175,177],abs=.2)
