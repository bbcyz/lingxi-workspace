import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from member_benefits_risk_runtime import (
    DFP_SDK_SHA256,
    WHEEL_GLUE_SHA256,
    WHEEL_WASM_SHA256,
    run_dfp,
    run_wheel,
)


class RiskRuntimeTests(unittest.TestCase):
    def test_android_import_does_not_load_the_desktop_v8_package(self):
        code = ('import sys; import member_benefits_risk_runtime as runtime; '
                'assert runtime.ANDROID; assert "py_mini_racer" not in sys.modules')
        subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                       env={**os.environ, 'LINGXI_ANDROID': '1'}, check=True)

    def test_dfp_mock_runs_pinned_sdk_and_persists_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "dfp-state.json"
            first = run_dfp(mock_server=True, state_path=state)
            second = run_dfp(mock_server=True, state_path=state, emit_token=False)

        self.assertTrue(first["ok"])
        self.assertEqual(first["sdk"]["sha256"], DFP_SDK_SHA256)
        self.assertEqual(first["result"]["gr-risk-p3"], "6af70000" + "m" * 32)
        self.assertEqual([item["business_status"] for item in first["requests"]], [-6, 2])
        self.assertTrue(first["state"]["persisted"])
        self.assertTrue(second["state"]["cache_reused"])
        self.assertNotIn("gr-risk-p3", second["result"])

    def test_wheel_mock_runs_pinned_wasm_without_node(self):
        result = run_wheel(
            "t" * 36,
            mock_gettoken=True,
            wheel_min_lifetime_ms=0,
        )
        values = result["roto_simple"]["value"]
        decoded = [len(base64.b64decode(value, validate=True)) for value in values]
        self.assertEqual(len(values[0]), 344)
        self.assertEqual(decoded[0], 256)
        self.assertGreaterEqual(decoded[1], 518)
        self.assertLessEqual(decoded[1], 532)
        self.assertGreaterEqual(result["calls"][0]["bodyLen"], 8000)
        self.assertEqual(result["calls"][0]["responseCode"], "0")
        self.assertEqual(result["module_url_kind"], "embedded-v8")
        self.assertEqual(result["assets"]["glue_sha256"], WHEEL_GLUE_SHA256)
        self.assertEqual(result["assets"]["wasm_sha256"], WHEEL_WASM_SHA256)

    def test_wheel_user_agent_and_page_are_configurable(self):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/157.0"
        page = "https://dev.coc.10086.cn/example?pageKey=3"
        result = run_wheel(
            "t" * 36,
            ua,
            page,
            mock_gettoken=True,
            wheel_min_lifetime_ms=0,
        )
        self.assertEqual(result["config"]["user_agent"], ua)
        self.assertEqual(result["config"]["page_url"], page)

    def test_wheel_uses_python_http_callback(self):
        seen = []

        def bridge(payload):
            seen.append(payload)
            return {
                "status": 200,
                "headers": {"content-type": "application/json"},
                "body": '{"code":"0","data":"00000000000000000000000000000000","success":true}',
                "transport": "python-test",
            }

        result = run_wheel(
            "t" * 36,
            wheel_min_lifetime_ms=0,
            http_bridge=bridge,
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["method"], "POST")
        self.assertEqual(result["calls"][0]["transport"], "python-test")


if __name__ == "__main__":
    unittest.main()
