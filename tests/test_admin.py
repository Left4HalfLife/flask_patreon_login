from datetime import timedelta
from io import BytesIO
from pathlib import Path
import time

import pyotp
import pytest
from PIL import Image
from werkzeug.security import check_password_hash

from conftest import ADMIN_PASSWORD, TOTP_SECRET, form_post, login_as
from patron_blog import initialize
from patron_blog.models import LoginSession, Post, Tag, Tier, User, db, now


def test_bootstrap_totp_and_forced_password_change(app, client, monkeypatch):
    fixed_time = int(time.time()) // 30 * 30 + 10
    monkeypatch.setattr("patron_blog.views.time.time", lambda: fixed_time)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("ADMIN_TOTP_SECRET", TOTP_SECRET)
    initialize()
    admin = db.session.scalar(db.select(User).where(User.is_admin.is_(True)))
    assert admin.must_change_password
    assert check_password_hash(admin.password_hash, ADMIN_PASSWORD)
    assert (
        form_post(
            client,
            "/settings",
            {
                "username": "admin",
                "password": ADMIN_PASSWORD,
                "totp": "invalid",
            },
        ).status_code
        == 400
    )
    code = pyotp.TOTP(TOTP_SECRET).at(fixed_time)
    response = form_post(
        client,
        "/settings",
        {
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "totp": code,
        },
    )
    assert response.status_code == 302
    assert client.get("/settings").location.endswith("/settings/password")
    assert client.get("/settings/posts/new").location.endswith("/settings/password")
    secret = Post(title="hidden", body="secret")
    db.session.add(secret)
    db.session.commit()
    assert client.get(f"/posts/{secret.id}").status_code == 404
    assert (
        form_post(
            client,
            "/settings/password",
            {
                "current_password": ADMIN_PASSWORD,
                "new_password": ADMIN_PASSWORD,
                "confirm_password": ADMIN_PASSWORD,
            },
        ).status_code
        == 400
    )
    new = "a-new-unique-password-for-tests"
    assert (
        form_post(
            client,
            "/settings/password",
            {
                "current_password": ADMIN_PASSWORD,
                "new_password": new,
                "confirm_password": new,
            },
        ).status_code
        == 302
    )
    assert not admin.must_change_password
    assert check_password_hash(admin.password_hash, new)
    assert client.get("/settings/posts/new").status_code == 200
    assert client.get(f"/posts/{secret.id}").status_code == 200
    assert db.session.scalar(db.select(db.func.count()).select_from(LoginSession)) == 1
    # Neither a restart nor an environment edit resets an existing admin.
    monkeypatch.setenv("ADMIN_PASSWORD", "changed-environment-password")
    initialize()
    assert check_password_hash(admin.password_hash, new)
    form_post(client, "/logout")
    replay = form_post(
        client,
        "/settings",
        {
            "username": "admin",
            "password": new,
            "totp": code,
        },
    )
    assert replay.status_code == 400
    assert b"already been used" in replay.data


def test_login_rate_limit_and_totp_required(client, admin):
    for _ in range(5):
        response = form_post(
            client,
            "/settings",
            {
                "username": "admin",
                "password": ADMIN_PASSWORD,
                "totp": "",
            },
        )
        assert response.status_code == 400
    response = form_post(
        client,
        "/settings",
        {
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "totp": pyotp.TOTP(TOTP_SECRET).now(),
        },
    )
    assert response.status_code == 429
    assert db.session.scalar(db.select(db.func.count()).select_from(LoginSession)) == 0


def test_create_edit_delete_post_tags_tiers_and_releases(app, client, admin):
    login_as(client, admin)
    assert form_post(client, "/settings/tags", {"name": "Sketches"}).status_code == 302
    assert form_post(client, "/settings/tags", {"name": "Sketches"}).status_code == 400
    assert (
        form_post(
            client,
            "/settings/tiers",
            {
                "tier_id": "123",
                "title": "Supporter",
                "amount_cents": "500",
            },
        ).status_code
        == 302
    )
    assert (
        form_post(
            client,
            "/settings/tiers",
            {
                "tier_id": "not-an-id",
                "title": "Invalid",
            },
        ).status_code
        == 400
    )
    tag = db.session.scalar(db.select(Tag))
    release_at = (now() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
    response = form_post(
        client,
        "/settings/posts/new",
        {
            "title": "First post",
            "body": "Hello!",
            "tier_ids": ["123"],
            "tags": [str(tag.id)],
            "release_at": [release_at],
            "release_public_0": "on",
            "attachments": (BytesIO(b"private bytes"), "my-file.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    post = db.session.scalar(db.select(Post))
    assert post.tier_ids == ["123"]
    assert post.tags == [tag]
    assert post.releases[0].public
    assert not post.public
    attachment = post.attachments[0]
    stored = Path(app.config["UPLOAD_FOLDER"]) / attachment.storage_name
    assert stored.read_bytes() == b"private bytes"
    assert stored.name != attachment.name
    assert client.get(response.location).status_code == 200
    editor = client.get(f"/settings/posts/{post.id}/edit")
    assert editor.status_code == 200
    assert b"+30 days" in editor.data
    response = form_post(
        client,
        f"/settings/posts/{post.id}/edit",
        {
            "title": "Updated",
            "body": "",
            "public": "on",
        },
    )
    assert response.status_code == 302
    assert post.public
    assert post.releases == []
    assert post.attachments
    assert form_post(client, f"/settings/posts/{post.id}/delete").status_code == 302
    assert not stored.exists()
    assert db.session.scalar(db.select(Post)) is None


def test_upload_validation_and_atomic_failure(app, client, admin):
    login_as(client, admin)
    image = BytesIO()
    Image.new("RGB", (4, 4), "red").save(image, format="PNG")
    image.seek(0)
    response = form_post(
        client,
        "/settings/posts/new",
        {
            "title": "Good image",
            "public": "on",
            "attachments": (image, "../../photo.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    post = db.session.scalar(db.select(Post))
    attachment = post.attachments[0]
    assert attachment.name == "photo.png"
    media = client.get(f"/media/{attachment.id}")
    assert media.mimetype == "image/png"
    assert "inline" in media.headers["Content-Disposition"]
    folder = Path(app.config["UPLOAD_FOLDER"])
    before = set(folder.iterdir())
    for filename, data in [
        ("attack.svg", b"<svg/>"),
        ("fake.png", b"<script/>"),
        ("fake.mp4", b"not a video"),
    ]:
        response = form_post(
            client,
            "/settings/posts/new",
            {
                "title": "Bad upload",
                "attachments": [
                    (BytesIO(b"ok"), "fine.txt"),
                    (BytesIO(data), filename),
                ],
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert set(folder.iterdir()) == before
    assert db.session.scalar(db.select(db.func.count()).select_from(Post)) == 1
    assert (
        form_post(client, f"/settings/media/{attachment.id}/delete").status_code == 302
    )
    assert not list(folder.iterdir())


def test_invalid_access_form_does_not_create_post(client, admin):
    login_as(client, admin)
    db.session.add(Tier(id="1", title="Tier", amount_cents=100))
    db.session.commit()
    for extra in [
        {"tier_ids": "unknown"},
        {"release_at": "not-a-date", "release_tiers_0": "1"},
        {"release_at": "2027-01-01T10:00"},
        {"release_at": "", "release_public_0": "on"},
        {"tags": "999"},
    ]:
        assert (
            form_post(
                client, "/settings/posts/new", {"title": "Bad", **extra}
            ).status_code
            == 400
        )
    assert db.session.scalar(db.select(Post)) is None


def test_request_size_limit(app, client, admin):
    login_as(client, admin)
    app.config["MAX_CONTENT_LENGTH"] = 1024
    response = form_post(
        client,
        "/settings/posts/new",
        {
            "title": "Too large",
            "attachments": (BytesIO(b"x" * 2048), "large.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert db.session.scalar(db.select(Post)) is None


def test_filesystem_failure_rolls_back_post_and_new_files(
    app, client, admin, monkeypatch
):
    from patron_blog import views

    login_as(client, admin)
    original_save = views.save_upload
    saved = []

    def failing_save(upload, metadata):
        if saved:
            raise OSError("simulated disk failure")
        result = original_save(upload, metadata)
        saved.append(result)
        return result

    monkeypatch.setattr(views, "save_upload", failing_save)
    with pytest.raises(OSError, match="simulated disk failure"):
        form_post(
            client,
            "/settings/posts/new",
            {
                "title": "Never committed",
                "attachments": [
                    (BytesIO(b"first"), "one.txt"),
                    (BytesIO(b"second"), "two.txt"),
                ],
            },
            content_type="multipart/form-data",
        )
    assert not list(Path(app.config["UPLOAD_FOLDER"]).iterdir())
    assert db.session.scalar(db.select(Post)) is None
