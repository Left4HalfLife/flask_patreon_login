import re
import secrets
from datetime import timedelta

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from patron_blog import create_app
from patron_blog.auth import digest
from patron_blog.models import LoginSession, User, db, now

ADMIN_PASSWORD = "temporary-password-for-tests"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key-not-for-deployment-" * 3,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.sqlite3'}",
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "SITE_URL": "http://localhost",
            "SESSION_COOKIE_SECURE": False,
            "PATREON_CLIENT_ID": "client-id",
            "PATREON_CLIENT_SECRET": "client-secret",
            "PATREON_CAMPAIGN_ID": "campaign-1",
        }
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin():
    user = User(
        name="admin",
        is_admin=True,
        password_hash=generate_password_hash(ADMIN_PASSWORD),
        totp_secret=TOTP_SECRET,
        must_change_password=False,
    )
    db.session.add(user)
    db.session.commit()
    return user


def login_as(client, user, expires_at=None):
    db.session.add(user)
    db.session.commit()
    token = secrets.token_urlsafe(32)
    db.session.add(
        LoginSession(
            user=user,
            token_hash=digest(token),
            expires_at=expires_at or now() + timedelta(days=30),
        )
    )
    db.session.commit()
    with client.session_transaction() as browser:
        browser["token"] = token
    return token


def csrf(client, path="/"):
    # Tests hold an app context across requests; real requests get a fresh g.
    g.pop("csrf_token", None)
    response = client.get(path)
    return re.search(rb'name="csrf_token" value="([^"]+)"', response.data)[1].decode()


def form_post(client, url, data=None, **kwargs):
    data = dict(data or {})
    data["csrf_token"] = csrf(client)
    return client.post(url, data=data, **kwargs)
