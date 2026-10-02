import asyncio
import httpx
from maasagentharness.dashboard.app import app


async def test_dashboard_routes():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Test SPA / index.html
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "MaaS" in resp.text or "<div id=\"root\">" in resp.text
        print("GET / (SPA) verified: 200 OK")

        # Test control states
        resp = await client.get("/control/user-a")
        assert resp.status_code == 200
        print("GET /control/user-a verified: 200 OK ->", resp.json())

        # Test Act 1 trigger
        resp = await client.post("/act/1")
        assert resp.status_code == 200
        print("POST /act/1 verified: 200 OK ->", resp.json()["act"])

        # Test Act 2 trigger
        resp = await client.post("/act/2")
        assert resp.status_code == 200
        assert resp.json()["control"]["user-a"]["state"] == "running"
        print("POST /act/2 verified: 200 OK (User A started)")

        # Test limit update
        resp = await client.post("/limits/team-alpha", json={"limit_tpm": 4000})
        assert resp.status_code == 200
        assert resp.json()["limit_tpm"] == 4000
        print("POST /limits/team-alpha verified: 200 OK")

        # Test event post
        event_payload = {
            "user": "user-a",
            "team": "team-alpha",
            "agent": "triage",
            "mode": "runaway",
            "run_id": "test-123",
            "node": "classify",
            "status": 429,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "latency_ms": 150,
            "ratelimit_headers": {"retry-after": "5"},
            "attempt": 1,
        }
        resp = await client.post("/events", json=event_payload)
        assert resp.status_code == 200
        print("POST /events verified: 200 OK")


if __name__ == "__main__":
    asyncio.run(test_dashboard_routes())
