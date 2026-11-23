from datetime import timedelta
from pathlib import Path

from conftest import form_post, login_as

from patron_blog.auth import can_view, digest
from patron_blog.models import (
    Attachment,
    LoginSession,
    Post,
    Release,
    Tag,
    User,
    db,
    now,
)


def make_post(**kwargs):
    post = Post(title="SECRET TITLE", body="PRIVATE BODY", **kwargs)
    db.session.add(post)
    db.session.commit()
    return post


def test_private_post_and_media_do_not_leak(app, client):
    post = make_post(tier_ids=["12"])
    attachment = Attachment(
        post=post,
        storage_name="random",
        name="private.txt",
        mime="text/plain",
        kind="file",
    )
    db.session.add(attachment)
    db.session.commit()
    (Path(app.config["UPLOAD_FOLDER"]) / "random").write_text("private file bytes")
    response = client.get("/")
    assert response.status_code == 200
    assert b"SECRET TITLE" not in response.data
    assert b"PRIVATE BODY" not in response.data
    assert client.get(f"/posts/{post.id}").status_code == 404
    assert client.get(f"/media/{attachment.id}").status_code == 404
    # Exact IDs only: "2" must not match tier "12".
    user = User(name="Wrong tier", patreon_id="other", tier_ids=["2"])
    login_as(client, user)
    assert b"SECRET TITLE" not in client.get("/").data
    assert client.get(f"/media/{attachment.id}").status_code == 404
    user.tier_ids = ["12"]
    db.session.commit()
    assert b"SECRET TITLE" in client.get("/").data
    assert client.get(f"/posts/{post.id}").status_code == 200
    response = client.get(f"/media/{attachment.id}")
    assert response.data == b"private file bytes"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_scheduled_grants_are_additive_and_utc(client):
    instant = now()
    post = make_post(tier_ids=["top"])
    post.releases = [
        Release(at=instant + timedelta(days=30), tier_ids=["lower"], public=False),
        Release(at=instant + timedelta(days=90), tier_ids=[], public=True),
    ]
    db.session.commit()
    top = User(name="Top", tier_ids=["top"])
    lower = User(name="Lower", tier_ids=["lower"])
    assert can_view(post, top, instant)
    assert not can_view(post, lower, instant + timedelta(days=30, microseconds=-1))
    assert can_view(post, lower, instant + timedelta(days=30))
    assert can_view(post, top, instant + timedelta(days=31))
    assert not can_view(post, None, instant + timedelta(days=89))
    assert can_view(post, None, instant + timedelta(days=90))
    assert b"SECRET TITLE" not in client.get("/").data
    post.releases[1].at = instant - timedelta(seconds=1)
    db.session.commit()
    assert b"SECRET TITLE" in client.get("/").data
    assert client.get(f"/posts/{post.id}").status_code == 200


def test_video_range_requests_are_permission_checked(app, client):
    post = make_post(tier_ids=["12"])
    content = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100
    attachment = Attachment(
        post=post, storage_name="video", name="clip.mp4", mime="video/mp4", kind="video"
    )
    db.session.add(attachment)
    db.session.commit()
    (Path(app.config["UPLOAD_FOLDER"]) / "video").write_bytes(content)
    url = f"/media/{attachment.id}"
    assert client.get(url, headers={"Range": "bytes=0-15"}).status_code == 404
    login_as(client, User(name="Video member", patreon_id="video", tier_ids=["12"]))
    response = client.get(url, headers={"Range": "bytes=0-15"})
    assert response.status_code == 206
    assert response.data == content[:16]
    assert response.headers["Content-Range"] == f"bytes 0-15/{len(content)}"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_released_lower_tier_is_listed_without_other_tiers(client):
    post = make_post(tier_ids=["high"])
    post.releases = [Release(at=now() - timedelta(seconds=1), tier_ids=["low"])]
    db.session.commit()
    login_as(client, User(name="Lower", patreon_id="low", tier_ids=["low"]))
    assert b"SECRET TITLE" in client.get("/").data
    assert client.get(f"/posts/{post.id}").status_code == 200
    assert b"SECRET TITLE" not in client.get("/", query_string={"tag": "absent"}).data


def test_public_posts_tags_and_autoescaping(client):
    tag = Tag(name="Artwork")
    post = make_post(public=True)
    post.title = "<script>title()</script>"
    post.body = "<img src=x onerror=alert(1)>"
    post.tags = [tag]
    db.session.commit()
    response = client.get(f"/posts/{post.id}")
    assert b"<script>title()" not in response.data
    assert b"&lt;script&gt;" in response.data
    assert b"<img src=x" not in response.data
    assert b"&lt;img" in response.data
    assert b"&lt;script&gt;" in client.get("/?tag=Artwork").data
    assert b"&lt;script&gt;" not in client.get("/?tag=Other").data


def test_fixed_session_expiry_and_logout(client):
    post = make_post(tier_ids=["12"])
    user = User(name="Member", patreon_id="id", tier_ids=["12"])
    token = login_as(client, user, now() + timedelta(seconds=5))
    initial = db.session.get(LoginSession, digest(token)).expires_at
    assert client.get(f"/posts/{post.id}").status_code == 200
    assert db.session.get(LoginSession, digest(token)).expires_at == initial
    db.session.get(LoginSession, digest(token)).expires_at = now() - timedelta(
        seconds=1
    )
    db.session.commit()
    assert client.get(f"/posts/{post.id}").status_code == 404
    token = login_as(client, user)
    assert form_post(client, "/logout").status_code == 302
    assert db.session.get(LoginSession, digest(token)) is None
    assert client.get(f"/posts/{post.id}").status_code == 404


def test_security_headers_csrf_and_hosts(client):
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert client.post("/settings", data={}).status_code == 400
    assert client.post("/auth/patreon").status_code == 400
    assert client.post("/logout").status_code == 400
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_admin_only_routes_for_members(client):
    login_as(client, User(name="Member", patreon_id="id", tier_ids=[]))
    assert client.get("/settings/posts/new").status_code == 403
    assert form_post(client, "/settings/tags", {"name": "forbidden"}).status_code == 403
    assert form_post(client, "/settings/tiers", {"tier_id": "1"}).status_code == 403
