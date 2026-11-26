import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "publish-site.py"


class PublishSiteTests(unittest.TestCase):
    def test_integrates_frontend_api_and_nginx_idempotently(self):
        temp_parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        with tempfile.TemporaryDirectory(prefix="jobhunter-publish-test-", dir=temp_parent) as temp:
            site = Path(temp) / "site"
            (site / "api").mkdir(parents=True)
            (site / "deploy").mkdir()
            app = site / "api" / "app.py"
            app.write_text(
                "from fastapi import FastAPI\n"
                "from starlette.middleware.trustedhost import TrustedHostMiddleware\n"
                "app = FastAPI(title=\"Example API\")\n"
                "app.add_middleware(TrustedHostMiddleware, allowed_hosts=[\"testserver\"])\n",
                encoding="utf-8",
            )
            nginx = site / "deploy" / "nginx-example-api.conf"
            nginx.write_text(
                "limit_req_zone $binary_remote_addr zone=example_api:10m rate=240r/m;\n"
                "location ~ ^/api/v1/(register|login|account/) {\n"
                "    proxy_pass http://example_api;\n"
                "}\n"
                "location / { proxy_pass http://example_api; }\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(site)], check=True)
            subprocess.run(["git", "-C", str(site), "config", "user.name", "Demo"], check=True)
            subprocess.run(["git", "-C", str(site), "config", "user.email", "demo@example.test"], check=True)
            env = dict(os.environ)
            env["JOBHUNTER_SITE_REPO"] = str(site)
            env["JOBHUNTER_PUBLIC_API_URL"] = "https://api.example.test/api/v1/jobhunter"
            for _ in range(2):
                result = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)

            patched_app = app.read_text(encoding="utf-8")
            patched_nginx = nginx.read_text(encoding="utf-8")
            self.assertEqual(1, patched_app.count("from api.jobhunter import router as jobhunter_router"))
            self.assertEqual(1, patched_app.count("app.include_router(jobhunter_router)"))
            self.assertEqual(1, patched_nginx.count("limit_req_zone $binary_remote_addr zone=jobhunter_visits"))
            self.assertEqual(1, patched_nginx.count("location = /api/v1/jobhunter/visit"))
            self.assertEqual(1, patched_nginx.count("jobhunter/login"))
            config = (site / "jobhunter" / "config.js").read_text(encoding="utf-8")
            self.assertIn("https://api.example.test/api/v1/jobhunter", config)
            self.assertTrue((site / "jobhunter" / "index.html").is_file())
            self.assertTrue((site / "api" / "jobhunter.py").is_file())
            self.assertNotIn("JOBHUNTER_PASSCODE", config)


if __name__ == "__main__":
    unittest.main()
