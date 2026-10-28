"""Exercise real corner detection/calibration and native frame preservation."""
import importlib.util
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]


def script():
    spec = importlib.util.spec_from_file_location("camera_calibration", ROOT/"scripts/calibrate_camera.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_multiview_chessboard_calibration_and_duplicate_rejection(tmp_path):
    cv2, np = pytest.importorskip("cv2"), pytest.importorskip("numpy")
    board = np.zeros((600, 900), np.uint8)
    for y in range(6):
        for x in range(9):
            board[y*100:(y+1)*100, x*100:(x+1)*100] = 255*((x+y) % 2)
    matrix = np.array([[780,0,319.5],[0,800,239.5],[0,0,1]], float)
    # Object origin is the first inside corner, one square from outer edges.
    object_corners = np.array([[-.02,-.02,0],[.16,-.02,0],[.16,.10,0],[-.02,.10,0]],float)
    source_edges = np.array([[-.5,-.5],[899.5,-.5],[899.5,599.5],[-.5,599.5]],np.float32)
    for i in range(16):
        rvec = np.array([-.45+.3*(i%4),-.4+.27*((i//4)%4),-.18+.12*((i*3)%4)],float)
        rotation, _ = cv2.Rodrigues(rvec)
        z = .48+.06*(i%4)
        target_pixel = np.array([190+85*(i%4),150+60*((i//4)%4)])
        target_center = np.array([(target_pixel[0]-319.5)*z/780,(target_pixel[1]-239.5)*z/800,z])
        tvec = target_center-rotation @ np.array([.07,.04,0])
        projected, _ = cv2.projectPoints(object_corners,rvec,tvec,matrix,np.zeros(5))
        pixels = projected.reshape(4,2).astype(np.float32)
        assert np.all((pixels[:,0]>0)&(pixels[:,0]<640)&(pixels[:,1]>0)&(pixels[:,1]<480))
        transform = cv2.getPerspectiveTransform(source_edges,pixels*4)
        high = cv2.warpPerspective(board,transform,(2560,1920),flags=cv2.INTER_NEAREST,borderValue=255)
        image = cv2.resize(high,(640,480),interpolation=cv2.INTER_AREA)
        assert cv2.imwrite(str(tmp_path/f"view-{i:02d}.png"),image)
    (tmp_path/"duplicate.png").write_bytes((tmp_path/"view-00.png").read_bytes())
    result = script().calibrate(tmp_path)
    assert result["image_size"] == [640,480]
    assert result["calibration"]["accepted_views"] == 16
    assert len(result["calibration"]["rejected"]) == 1
    assert result["calibration"]["overall_rms_px"] < .5
    assert result["camera_matrix"][0][0] == pytest.approx(780,rel=.03)
    assert result["camera_matrix"][1][1] == pytest.approx(800,rel=.03)
    assert result["calibration"]["needs_physical_validation"] is True


def test_capture_preserves_native_size_orientation_and_rejects_old_frames():
    cv2, np = pytest.importorskip("cv2"), pytest.importorskip("numpy")
    module = script()
    raw = np.zeros((4,6,2),np.uint8)
    raw[:,:,0] = 128
    raw[:,:,1] = np.arange(24,dtype=np.uint8).reshape(4,6)*9+16
    headers = {"X-Frame-Format":"UYVY","X-Frame-Width":"6","X-Frame-Height":"4",
               "X-Frame-Rotate":"180","X-Frame-Id":"1000","X-Frame-Captured-At":"1000",
               "X-Frame-Age-S":".1"}
    response = httpx.Response(200,content=raw.tobytes(),headers=headers,request=httpx.Request("GET","http://robot/frame"))
    png, meta = module.frame_to_png(response,.2)
    observed = cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_COLOR)
    expected = cv2.cvtColor(raw,cv2.COLOR_YUV2BGR_UYVY)
    assert np.array_equal(observed,expected)
    assert meta["image_size"] == [6,4]
    assert meta["pixel_rotation_applied_degrees"] == 0
    assert meta["source_display_rotation_degrees"] == 180
    with pytest.raises(ValueError,match="stale"):
        module.frame_to_png(response,1.0)
    response.headers["X-Frame-Age-S"] = "nan"
    with pytest.raises(ValueError,match="stale"):
        module.frame_to_png(response,.1)


def test_calibration_does_not_accept_empty_or_mismatched_images(tmp_path):
    cv2, np = pytest.importorskip("cv2"), pytest.importorskip("numpy")
    module = script()
    with pytest.raises(ValueError,match="No PNG"):
        module.calibrate(tmp_path)
    cv2.imwrite(str(tmp_path/"one.png"),np.full((400,600),255,np.uint8))
    with pytest.raises(ValueError,match="at least 12"):
        module.calibrate(tmp_path)
    cv2.imwrite(str(tmp_path/"two.png"),np.full((480,640),255,np.uint8))
    with pytest.raises(ValueError,match="size mismatch"):
        module.calibrate(tmp_path)
