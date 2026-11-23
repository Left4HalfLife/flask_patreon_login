import os
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import click
import pyotp
from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.exceptions import SecurityError
from werkzeug.security import generate_password_hash

from .models import LoginSession, RateLimit, User, db, now


@event.listens_for(Engine, "connect")
def sqlite_settings(connection, _):
    if isinstance(connection, sqlite3.Connection):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def cleanup():
    cutoff = now() - timedelta(days=90)
    users = db.session.execute(
        db.delete(User).where(User.is_admin.is_(False), User.last_login < cutoff)
    ).rowcount
    db.session.execute(db.delete(LoginSession).where(LoginSession.expires_at <= now()))
    db.session.execute(
        db.delete(RateLimit).where(RateLimit.window < int(time.time()) // 900 - 1)
    )
    db.session.commit()
    return users


def initialize():
    db.create_all()
    db.session.execute(db.text("PRAGMA journal_mode=WAL"))
    if not db.session.scalar(db.select(User).where(User.is_admin.is_(True))):
        password = os.environ.get("ADMIN_PASSWORD", "")
        username = os.environ.get("ADMIN_USERNAME", "admin").strip()
        secret = os.environ.get("ADMIN_TOTP_SECRET", "").replace(" ", "").upper()
        if not 14 <= len(password) <= 256:
            raise click.ClickException("ADMIN_PASSWORD must contain 14-256 characters.")
        if not 1 <= len(username) <= 200:
            raise click.ClickException("ADMIN_USERNAME must contain 1-200 characters.")
        try:
            if len(secret) < 32:
                raise ValueError
            pyotp.TOTP(secret).at(0)
        except (ValueError, TypeError) as exc:
            raise click.ClickException(
                "ADMIN_TOTP_SECRET must be a valid 32+ character base32 secret."
            ) from exc
        db.session.add(
            User(
                name=username,
                is_admin=True,
                password_hash=generate_password_hash(password),
                totp_secret=secret,
                must_change_password=True,
            )
        )
    db.session.commit()
    cleanup()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    data_dir = Path(os.environ.get("DATA_DIR", app.instance_path)).resolve()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", ""),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{data_dir / 'blog.sqlite3'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOAD_FOLDER=str(data_dir / "uploads"),
        MAX_CONTENT_LENGTH=256 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=512 * 1024,
        MAX_FORM_PARTS=200,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_REFRESH_EACH_REQUEST=False,
        SITE_URL=os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/"),
        SITE_NAME=os.environ.get("SITE_NAME", "The Members' Studio"),
        PATREON_CLIENT_ID=os.environ.get("PATREON_CLIENT_ID", ""),
        PATREON_CLIENT_SECRET=os.environ.get("PATREON_CLIENT_SECRET", ""),
        PATREON_CAMPAIGN_ID=os.environ.get("PATREON_CAMPAIGN_ID", ""),
        PATREON_CREATOR_ACCESS_TOKEN=os.environ.get("PATREON_CREATOR_ACCESS_TOKEN", ""),
        DUMMY_PASSWORD_HASH=generate_password_hash("not-a-real-administrator-password"),
    )
    if test_config:
        app.config.update(test_config)
    if len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError(
            "Set SECRET_KEY to a random secret of at least 32 characters."
        )
    site = urlparse(app.config["SITE_URL"])
    if (
        site.scheme not in {"https", "http"}
        or not site.hostname
        or site.username
        or site.password
        or site.query
        or site.fragment
        or site.path
        or (app.config["SESSION_COOKIE_SECURE"] and site.scheme != "https")
    ):
        raise RuntimeError("SITE_URL must be an origin; use HTTPS with secure cookies.")
    app.config["TRUSTED_HOSTS"] = [site.hostname]
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:////"):
        database_path = Path(
            app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")
        )
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    db.init_app(app)
    CSRFProtect(app)

    from .views import bp

    app.register_blueprint(bp)

    @app.after_request
    def secure_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; media-src 'self'; "
            "script-src 'self'; style-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Cache-Control"] = "private, no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    @app.errorhandler(502)
    @app.errorhandler(503)
    def http_error(error):
        return render_template("error.html", error=error), error.code

    @app.errorhandler(SecurityError)
    def untrusted_host(error):
        # Host rejection happens before Flask has a URL adapter for templates.
        return error.get_response()

    @app.cli.command("init-db")
    def init_db_command():
        """Create tables and bootstrap the admin once from the environment."""
        initialize()
        click.echo("Database initialized.")

    @app.cli.command("cleanup")
    @click.option(
        "--loop", is_flag=True, help="Run hourly, for the Compose maintenance service."
    )
    def cleanup_command(loop):
        """Delete non-admin accounts inactive for 90 days and expired tokens."""
        while True:
            click.echo(f"Deleted {cleanup()} inactive accounts.")
            if not loop:
                break
            db.session.remove()
            time.sleep(3600)

    return app
