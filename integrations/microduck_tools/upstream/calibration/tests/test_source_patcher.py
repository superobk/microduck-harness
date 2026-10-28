"""Structural tests for strict source patch logic (real Rust toolchain requires user's build host)."""
import importlib.util
import unittest
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'common'/'apply_source_patch.py'
spec=importlib.util.spec_from_file_location('apply_source_patch',path)
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class SourcePatcherTests(unittest.TestCase):
    def test_params_both_pinned_targets(self):
        src='''pub struct Bus {\n    pub fast_sync_read: bool,\n}\nimpl Default for Bus {\n    fn default() -> Self {\n        Self {\n            fast_sync_read: true,\n        }\n    }\n}\nimpl Default for Control {\n}\npub enum ParamsError {\n    Rate { path: String, got: u32 },\n}\n    fn validate(&self, path: &Path) -> Result<(), ParamsError> {\n}\n'''
        for tag in sorted(p.SUPPORTED):
            with self.subTest(tag=tag):
                patched=p.patch_params(src,tag)
                self.assertIn('pub calibration_path: String',patched)
                self.assertIn('bus.load_calibration()',patched)
                self.assertIn(f'target_tag != "{tag}"',patched)
                self.assertIn('joint_zero_rad',patched)

    def test_registry(self):
        source='    // ── [control] ────────────────────────────────────────────────────────────'
        self.assertIn('bus.calibration_path',p.patch_registry(source))

    def test_bus_conversion_anchors(self):
        source='''pub struct DynamixelIo {\n    imu: SflpDecoder,\n    /// Blocks identical\n}\n    pub fn open(port: &str, fast_sync_read: bool) -> Result<Self> {\n        Ok(Self {\n            imu: SflpDecoder::default(),\n        })\n    }\n    /// Returns how many needed fixing.\n    pub fn present_positions(&mut self) -> Result<[f64; NUM_JOINTS]> {\n        out.copy_from_slice(&values);\n        Ok(out)\n    }\nimpl RobotIo for DynamixelIo {\n  fn read(&mut self) {\n            sensors.velocities[joint] = velocity as f64 * RAD_PER_SEC_PER_COUNT;\n            let position = i32::from_le_bytes([block[8], block[9], block[10], block[11]]);\n            sensors.positions[joint] = (2.0 * PI * position as f64 / 4096.0) - PI;\n  }\n  fn write(&mut self) {\n        self.controller\n            .sync_write_goal_position(&JOINT_IDS, &targets.positions)\n            .map_err(|e| IoError::Bus(format!("sync_write goal positions: {e}")))\n  }\n}\n'''
        patch=p.patch_bus(source)
        self.assertIn('self.joint_calibration.to_robot(joint, physical)',patch)
        self.assertIn('self.joint_calibration.to_robot_velocity',patch)
        self.assertIn('self.joint_calibration.to_motor',patch)

    def test_robotd_anchor(self):
        s=('    io.set_imu_mount(body_imu_mount(board));\n'
           '                    state.startup_imu_missing.store(false, Ordering::Relaxed);\n'
           '                    return Some(io);\n'
           '/// Open and verify the bus, or explain why not.\n'
           '/// The body IMU')
        t=p.patch_main(s)
        self.assertIn('bus.load_calibration()',t)
        self.assertIn('io.set_joint_calibration',t)
        self.assertIn('write_effective_calibration',t)
        self.assertIn('gyro_raw_counts_per_dps',t)

    def test_imu_decoder_patcher(self):
        src = ("pub struct SflpDecoder {\n    mount: [f64; 4],\n}\n"
               "impl SflpDecoder {\n pub fn new(mount: [f64;4]) -> Self {\n"
               "Self {\n            mount,\n            last_quat: [1.0, 0.0, 0.0, 0.0],\n}\n}\n"
               "    pub fn decode(&mut self, raw: &[u8; IMU_BLOCK_LEN]) -> ImuData {\n"
               "let x = 1 as f64 * GYRO_RAD_PER_LSB;\n"
               "let y = 2 as f64 * GYRO_RAD_PER_LSB;\n"
               "let z = 3 as f64 * GYRO_RAD_PER_LSB;\n}\n}\n")
        result=p.patch_imu(src)
        self.assertEqual(result.count('as f64 * self.gyro_rad_per_lsb'),3)
        self.assertIn('gyro_rad_per_lsb: GYRO_RAD_PER_LSB',result)
        self.assertIn('conversion_is_radians_per_second',result)

    def test_idempotence_guard(self):
        with self.assertRaises(ValueError):p.patch_main('random rust source')

if __name__=='__main__':unittest.main()
