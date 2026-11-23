from datetime import timedelta

from conftest import form_post, login_as
from patron_blog import cleanup
from patron_blog.models import LoginSession, Post, User, db, now


def test_cleanup_90_day_boundary_admin_exemption_and_recreation(
    app, client, admin, monkeypatch
):
    instant = now()
    stale = User(
        name="Stale", patreon_id="returning", last_login=instant - timedelta(days=91)
    )
    boundary = User(
        name="Boundary", patreon_id="boundary", last_login=instant - timedelta(days=90)
    )
    recent = User(
        name="Recent", patreon_id="recent", last_login=instant - timedelta(days=89)
    )
    admin.last_login = instant - timedelta(days=400)
    db.session.add_all([stale, boundary, recent, Post(title="Keep me", body="kept")])
    db.session.commit()
    login_as(client, stale)
    monkeypatch.setattr("patron_blog.now", lambda: instant)
    assert cleanup() == 1
    db.session.expire_all()
    assert (
        db.session.scalar(db.select(User).where(User.patreon_id == "returning")) is None
    )
    assert db.session.get(User, boundary.id)
    assert db.session.get(User, recent.id)
    assert db.session.get(User, admin.id)
    assert db.session.scalar(db.select(LoginSession)) is None
    assert db.session.scalar(db.select(Post)).title == "Keep me"
    assert client.get("/").status_code == 200
    from patron_blog import patreon

    monkeypatch.setattr(
        patreon, "identity", lambda code: ("returning", "Recreated", ["12"])
    )
    response = form_post(client, "/auth/patreon")
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(response.location).query)["state"][0]
    assert (
        client.get(
            "/auth/patreon/callback", query_string={"state": state, "code": "valid"}
        ).status_code
        == 302
    )
    recreated = db.session.scalar(db.select(User).where(User.patreon_id == "returning"))
    assert recreated.name == "Recreated"
    assert recreated.tier_ids == ["12"]
    result = app.test_cli_runner().invoke(args=["cleanup"])
    assert result.exit_code == 0
