import base64
import json

import pytest

from george import google_auth
from george.tools import google, registry
from george.tools.common import ToolContext, ToolError


def make_ctx(role="owner", tenant_id="t-1"):
    return ToolContext(
        chat_id="1", role=role, user_name="Test", correlation_id="c", tenant_id=tenant_id,
        tenant={"timezone": "America/Bogota"},
    )


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture
def calls(monkeypatch):
    """Replaces the HTTP layer; records (method, url, kwargs) and returns queued payloads."""
    recorded, queue = [], []

    def fake_request(tenant_id, method, url, **kwargs):
        recorded.append((tenant_id, method, url, kwargs))
        return queue.pop(0) if queue else {}

    monkeypatch.setattr(google, "_request", fake_request)
    recorded_queue = type("C", (), {"calls": recorded, "queue": queue})
    return recorded_queue


@pytest.mark.parametrize("tool", [t.name for t in google.TOOLS])
def test_google_tools_registered_and_owner_admin_only(tool):
    spec = registry._BY_NAME[tool]
    assert spec.allowed_roles == ("owner", "admin")
    with pytest.raises(ToolError):
        spec.handler({"query": "", "message_id": "x", "time_min": "2026-11-03", "event_id": "e",
                      "summary": "s", "start": "2026-11-03", "end": "2026-11-03"}, make_ctx("walker"))


def test_requires_tenant():
    with pytest.raises(ToolError):
        google._gmail_search({}, make_ctx(tenant_id=None))


def test_gmail_read_prefers_plain_and_marks_untrusted(calls):
    calls.queue.append({
        "id": "m1",
        "payload": {
            "headers": [{"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "Hola"}],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
                {"mimeType": "text/plain", "body": {"data": b64("texto plano")}},
            ],
        },
    })
    out = json.loads(google._gmail_read({"message_id": "m1"}, make_ctx()))
    assert out["UNTRUSTED_EMAIL_BODY"] == "texto plano"
    assert out["subject"] == "Hola"
    assert calls.calls[0][0] == "t-1"


def test_gmail_read_falls_back_to_stripped_html(calls):
    calls.queue.append({"id": "m1", "payload": {"mimeType": "text/html",
                                                "body": {"data": b64("<style>x{}</style><b>Hola</b>&nbsp;mundo")}}})
    out = json.loads(google._gmail_read({"message_id": "m1"}, make_ctx()))
    assert "Hola" in out["UNTRUSTED_EMAIL_BODY"] and "<" not in out["UNTRUSTED_EMAIL_BODY"]


def test_create_event_with_meet_sets_conference_and_defaults_to_no_invites(calls):
    calls.queue.append({"id": "e1", "hangoutLink": "https://meet.google.com/abc-defg-hij",
                        "start": {"dateTime": "2026-11-03T15:00:00-05:00"}, "end": {}})
    out = json.loads(google._calendar_create_event(
        {"summary": "Reunión", "start": "2026-11-03T15:00:00", "end": "2026-11-03T16:00:00",
         "add_meet": True, "attendees": ["x@y.com"]}, make_ctx()))
    _, method, _, kwargs = calls.calls[0]
    assert method == "POST"
    assert kwargs["params"] == {"conferenceDataVersion": 1, "sendUpdates": "none"}
    body = kwargs["json"]
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert body["start"] == {"dateTime": "2026-11-03T15:00:00", "timeZone": "America/Bogota"}
    assert body["attendees"] == [{"email": "x@y.com"}]
    assert out["meet_link"] == "https://meet.google.com/abc-defg-hij"


def test_all_day_event_uses_date():
    assert google._time_obj("2026-11-03", "America/Bogota") == {"date": "2026-11-03"}


def test_list_events_adds_tenant_offset_to_naive_bounds(calls):
    calls.queue.append({"items": []})
    google._calendar_list_events({"time_min": "2026-11-03"}, make_ctx())
    assert calls.calls[0][3]["params"]["timeMin"] == "2026-11-03T00:00:00-05:00"


def test_update_without_changes_errors(calls):
    with pytest.raises(ToolError):
        google._calendar_update_event({"event_id": "e1"}, make_ctx())
    assert not calls.calls


# ---------------------------------------------------------------- google_auth


def test_secret_name_is_per_tenant():
    assert google_auth.secret_name("abc") == "google-abc"


def test_missing_vault_is_a_clear_error(monkeypatch):
    google_auth.clear_cache()
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth._load_credentials("t-1")  # KEY_VAULT_URI is unset under tests


def test_malformed_secret_reports_missing_fields(monkeypatch):
    google_auth.clear_cache()
    monkeypatch.setattr(google_auth, "settings", type("S", (), {"key_vault_uri": "https://kv"})())

    class Secret:
        value = json.dumps({"client_id": "a"})

    class Client:
        def get_secret(self, name):
            assert name == "google-t-1"
            return Secret()

    monkeypatch.setattr(google_auth, "_client", lambda: Client())
    with pytest.raises(google_auth.GoogleAuthError, match="client_secret"):
        google_auth._load_credentials("t-1")
