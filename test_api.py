import os
import sys
import tempfile
import unittest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class WinterDriveStressSuite(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        os.environ["WINTER_DRIVE_DB_PATH"] = self.tmp_db.name
        os.environ["ADMIN_PIN"] = "9876"
        import api.index as app_mod
        app_mod.SQLITE_PATH = self.tmp_db.name
        app_mod.ADMIN_PIN = "9876"
        self.client = TestClient(app_mod.app)

    def tearDown(self):
        if os.path.exists(self.tmp_db.name):
            os.unlink(self.tmp_db.name)

    def _register_family(self, phone, parent_count=1, kid_count=1, raffle=True, ref=None):
        r1 = self.client.post("/api/send-otp", json={"phone": phone})
        self.assertEqual(r1.status_code, 200, r1.text)
        code = r1.json()["demo_code"]

        r2 = self.client.post("/api/verify-otp", json={"phone": phone, "code": code})
        self.assertEqual(r2.status_code, 200, r2.text)
        token = r2.json()["verification_token"]

        parents = [
            {"name": f"Parent {i+1} ({phone[-4:]})", "coat": "L", "glove": "M", "hat": "One Size Adult"}
            for i in range(parent_count)
        ]
        kids = [
            {"name": f"Kid {i+1}", "coat": "Youth M", "glove": "Youth M", "hat": "Youth S/M"}
            for i in range(kid_count)
        ]
        r3 = self.client.post(
            "/api/register",
            json={
                "phone": phone,
                "verification_token": token,
                "referred_by": ref,
                "raffle_opt_in": raffle,
                "parents": parents,
                "kids": kids,
            },
        )
        self.assertEqual(r3.status_code, 200, r3.text)
        return r3.json()

    def test_20_runs_end_to_end_and_edge_cases(self):
        # 1. Health check
        h = self.client.get("/api/health")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.json()["status"], "ok")

        # 2. Invalid phone rejected
        bad_phone = self.client.post("/api/send-otp", json={"phone": "123"})
        self.assertEqual(bad_phone.status_code, 400)

        # 3. Wrong OTP rejected
        self.client.post("/api/send-otp", json={"phone": "7015550001"})
        bad_otp = self.client.post("/api/verify-otp", json={"phone": "7015550001", "code": "000000"})
        self.assertEqual(bad_otp.status_code, 400)

        # 4. Register 20 distinct families (varying parents 1-2, kids 0-3, raffle opt-in/out, referrals)
        f1 = self._register_family("701-555-0100", parent_count=2, kid_count=3, raffle=True)
        code1 = f1["share_code"]

        # Duplicate phone blocked at send-otp AND register
        dup = self.client.post("/api/send-otp", json={"phone": "+1 (701) 555-0100"})
        self.assertEqual(dup.status_code, 409)

        # Register 19 more families; first 4 use code1 -> code1 should get 2 bonus tickets (total 3)
        for idx in range(1, 20):
            phone = f"701555{1000 + idx:04d}"
            ref = code1 if idx <= 4 else None
            raffle = (idx % 5 != 0)
            self._register_family(
                phone,
                parent_count=1 + (idx % 2),
                kid_count=idx % 4,
                raffle=raffle,
                ref=ref,
            )

        # Verify referral math: 4 referrals -> 2 bonus tickets -> 3 total tickets
        lk = self.client.get(f"/api/lookup?q={code1}")
        self.assertEqual(lk.status_code, 200)
        self.assertEqual(lk.json()["referral_count"], 4)
        self.assertEqual(lk.json()["bonus_tickets"], 2)
        self.assertEqual(lk.json()["raffle_tickets"], 3)

        # Verify slot limits (>2 parents or >3 kids rejected)
        r_otp = self.client.post("/api/send-otp", json={"phone": "7015559999"}).json()
        tok = self.client.post("/api/verify-otp", json={"phone": "7015559999", "code": r_otp["demo_code"]}).json()["verification_token"]
        too_many_kids = self.client.post(
            "/api/register",
            json={
                "phone": "7015559999",
                "verification_token": tok,
                "raffle_opt_in": True,
                "parents": [{"name": "P1", "coat": "M", "glove": "M", "hat": "S/M"}],
                "kids": [{"name": f"K{i}", "coat": "4T", "glove": "Toddler", "hat": "Toddler"} for i in range(4)],
            },
        )
        self.assertEqual(too_many_kids.status_code, 422)

        # Admin auth check
        unauth = self.client.get("/api/admin/totals")
        self.assertEqual(unauth.status_code, 401)

        totals = self.client.get("/api/admin/totals", headers={"X-Admin-Pin": "9876"})
        self.assertEqual(totals.status_code, 200)
        self.assertEqual(totals.json()["total_families"], 20)

        # Draw PS5 & TV
        draw_ps5 = self.client.post("/api/admin/draw", json={"prize": "ps5"}, headers={"X-Admin-Pin": "9876"})
        self.assertEqual(draw_ps5.status_code, 200)
        draw_tv = self.client.post("/api/admin/draw", json={"prize": "tv"}, headers={"X-Admin-Pin": "9876"})
        self.assertEqual(draw_tv.status_code, 200)

        # Export CSV
        csv_res = self.client.get("/api/admin/export-csv", headers={"X-Admin-Pin": "9876"})
        self.assertEqual(csv_res.status_code, 200)
        self.assertIn("WGD-", csv_res.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
