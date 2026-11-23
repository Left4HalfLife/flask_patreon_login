import copy
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from conftest import form_post, login_as
from patron_blog import patreon
from patron_blog.auth import digest
from patron_blog.models import LoginSession, Tier, User, db, now


@pytest.fixture
def identity_payload():
    return {
        "data": {
            "type": "user",
            "id": "person-1",
            "attributes": {"full_name": "A member"},
            "relationships": {
                "memberships": {
                    "data": [
                        {"type": "member", "id": "ours"},
                        {"type": "member", "id": "theirs"},
                    ]
                }
            },
        },
        "included": [
            {
                "type": "member",
                "id": "ours",
                "attributes": {"patron_status": "active_patron"},
                "relationships": {
                    "campaign": {"data": {"type": "campaign", "id": "campaign-1"}},
                    "currently_entitled_tiers": {
                        "data": [{"type": "tier", "id": "12"}]
                    },
                },
            },
            {
                "type": "member",
                "id": "theirs",
                "attributes": {"patron_status": "active_patron"},
                "relationships": {
                    "campaign": {
                        "data": {"type": "campaign", "id": "another-campaign"}
                    },
                    "currently_entitled_tiers": {
                        "data": [{"type": "tier", "id": "999"}]
                    },
                },
            },
        ],
    }


def start_login(client):
    response = form_post(client, "/auth/patreon")
    assert response.status_code == 302
    query = parse_qs(urlparse(response.location).query)
    assert query["scope"] == ["identity identity.memberships"]
    assert query["redirect_uri"] == ["http://localhost/auth/patreon/callback"]
    return query["state"][0]


def test_campaign_and_entitled_tier_filtering(identity_payload):
    assert patreon.parse_identity(identity_payload, "campaign-1") == (
        "person-1",
        "A member",
        ["12"],
    )
    for status in ("former_patron", "declined_patron", None):
        payload = copy.deepcopy(identity_payload)
        payload["included"][0]["attributes"]["patron_status"] = status
        assert patreon.parse_identity(payload, "campaign-1")[2] == []
    # An included resource not linked to the identity cannot grant access.
    identity_payload["data"]["relationships"]["memberships"]["data"] = []
    assert patreon.parse_identity(identity_payload, "campaign-1")[2] == []
    with pytest.raises(patreon.PatreonError):
        patreon.parse_identity({"data": {"id": "1", "type": "user"}}, "campaign-1")


def test_missing_membership_data_is_an_error_not_a_free_login(identity_payload):
    identity_payload["included"] = []
    with pytest.raises(patreon.PatreonError):
        patreon.parse_identity(identity_payload, "campaign-1")


def test_free_identity_without_memberships_can_log_in(identity_payload):
    identity_payload["data"]["relationships"]["memberships"]["data"] = []
    identity_payload["included"] = []
    assert patreon.parse_identity(identity_payload, "campaign-1") == (
        "person-1",
        "A member",
        [],
    )


def test_oauth_http_contract(client, monkeypatch, identity_payload):
    calls = []

    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            pass

        def json(self):
            return self.value

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return (
            Response({"access_token": "transient-bearer"})
            if method == "POST"
            else Response(identity_payload)
        )

    monkeypatch.setattr(patreon.requests, "request", fake_request)
    state = start_login(client)
    response = client.get(
        "/auth/patreon/callback", query_string={"code": "one-use", "state": state}
    )
    assert response.status_code == 302
    assert calls[0][1] == "https://www.patreon.com/api/oauth2/token"
    assert calls[0][2]["data"] == {
        "grant_type": "authorization_code",
        "code": "one-use",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "redirect_uri": "http://localhost/auth/patreon/callback",
    }
    assert calls[1][1].endswith("/api/oauth2/v2/identity")
    assert calls[1][2]["headers"]["Authorization"] == "Bearer transient-bearer"
    assert calls[1][2]["headers"]["User-Agent"] == "PatreonMembersStudio/1.0"
    assert (
        calls[1][2]["params"]["include"]
        == "memberships.currently_entitled_tiers,memberships.campaign"
    )
    assert calls[1][2]["params"]["fields[member]"] == "patron_status"
    assert all(call[2]["allow_redirects"] is False for call in calls)
    user = db.session.scalar(db.select(User))
    assert user.patreon_id == "person-1"
    assert user.tier_ids == ["12"]
    login = db.session.scalar(db.select(LoginSession))
    assert timedelta(days=29, hours=23) < login.expires_at - now() <= timedelta(days=30)
    with client.session_transaction() as browser:
        assert "transient-bearer" not in str(dict(browser))
        assert login.token_hash == digest(browser["token"])
        assert "oauth_state" not in browser
    assert (
        client.get(
            "/auth/patreon/callback", query_string={"code": "one-use", "state": state}
        ).status_code
        == 400
    )


def test_login_updates_existing_user_downgrade_and_revokes_old_sessions(
    client, monkeypatch
):
    user = User(
        name="Old",
        patreon_id="person",
        tier_ids=["12"],
        last_login=now() - timedelta(days=20),
    )
    old_token = login_as(client, user)
    monkeypatch.setattr(patreon, "identity", lambda code: ("person", "New", []))
    state = start_login(client)
    assert (
        client.get(
            "/auth/patreon/callback", query_string={"code": "new", "state": state}
        ).status_code
        == 302
    )
    assert user.name == "New"
    assert user.tier_ids == []
    assert user.last_login > now() - timedelta(seconds=5)
    assert db.session.get(LoginSession, digest(old_token)) is None
    assert db.session.scalar(db.select(db.func.count()).select_from(User)) == 1


def test_oauth_rejects_bad_expired_and_cancelled_state(client, monkeypatch):
    def forbidden(code):
        pytest.fail("No token exchange should happen with an invalid callback.")

    monkeypatch.setattr(patreon, "identity", forbidden)
    assert (
        client.get("/auth/patreon/callback?state=attacker&code=nope").status_code == 400
    )
    state = start_login(client)
    with client.session_transaction() as browser:
        browser["oauth_started"] = time.time() - 601
    assert (
        client.get(
            "/auth/patreon/callback", query_string={"state": state, "code": "nope"}
        ).status_code
        == 400
    )
    state = start_login(client)
    assert (
        client.get(
            "/auth/patreon/callback",
            query_string={"state": state, "error": "access_denied"},
        ).status_code
        == 400
    )
    assert db.session.scalar(db.select(User)) is None


def test_non_ascii_oauth_state_is_rejected(client, monkeypatch):
    start_login(client)
    monkeypatch.setattr(patreon, "identity", lambda code: pytest.fail("Invalid state"))
    response = client.get(
        "/auth/patreon/callback", query_string={"state": "\u00e9", "code": "nope"}
    )
    assert response.status_code == 400


def test_api_failure_revokes_previous_browser_session(client, monkeypatch):
    user = User(name="Previous", patreon_id="person", tier_ids=["12"])
    token = login_as(client, user)

    def unavailable(*args, **kwargs):
        raise requests.Timeout("Do not expose HTTP internals")

    monkeypatch.setattr(patreon.requests, "request", unavailable)
    state = start_login(client)
    response = client.get(
        "/auth/patreon/callback", query_string={"code": "new", "state": state}
    )
    assert response.status_code == 502
    assert b"Do not expose HTTP internals" not in response.data
    assert db.session.get(LoginSession, digest(token)) is None


def test_tier_sync_contract(client, admin, monkeypatch, app):
    login_as(client, admin)
    app.config["PATREON_CREATOR_ACCESS_TOKEN"] = "creator-token"

    def fake_request(method, url, **kwargs):
        assert method == "GET"
        assert url.endswith("/campaigns/campaign-1")
        assert kwargs["headers"]["Authorization"] == "Bearer creator-token"
        assert kwargs["params"]["include"] == "tiers"

        class Response:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {
                        "type": "campaign",
                        "id": "campaign-1",
                        "relationships": {
                            "tiers": {"data": [{"type": "tier", "id": "12"}]}
                        },
                    },
                    "included": [
                        {
                            "type": "tier",
                            "id": "12",
                            "attributes": {"title": "Studio", "amount_cents": 500},
                        },
                    ],
                }

        return Response()

    monkeypatch.setattr(patreon.requests, "request", fake_request)
    assert form_post(client, "/settings/tiers/sync").status_code == 302
    assert db.session.get(Tier, "12").title == "Studio"
