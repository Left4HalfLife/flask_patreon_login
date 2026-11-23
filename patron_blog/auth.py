import hashlib
import secrets
import time
from datetime import timedelta
from functools import wraps

from flask import abort, g, redirect, request, session, url_for
from sqlalchemy.dialects.sqlite import insert

from .models import LoginSession, RateLimit, db, now


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def rate_limit(label, limit):
    window = int(time.time()) // 900
    key = digest(f"{label}:{request.remote_addr}:{window}")
    statement = insert(RateLimit).values(key=key, window=window, count=1)
    count = db.session.execute(
        statement.on_conflict_do_update(
            index_elements=["key"], set_={"count": RateLimit.count + 1}
        ).returning(RateLimit.count)
    ).scalar_one()
    db.session.commit()
    if count > limit:
        abort(429, "Too many attempts. Try again in 15 minutes.")


def load_user():
    g.user = None
    token = session.get("token")
    if not isinstance(token, str):
        return
    login = db.session.get(LoginSession, digest(token))
    if login and login.expires_at > now():
        g.user = login.user
    else:
        session.clear()


def revoke_session():
    token = session.get("token")
    if isinstance(token, str):
        db.session.execute(
            db.delete(LoginSession).where(LoginSession.token_hash == digest(token))
        )
    session.clear()


def sign_in(user):
    revoke_session()
    token = secrets.token_urlsafe(32)
    db.session.add(
        LoginSession(
            token_hash=digest(token),
            user=user,
            expires_at=now()
            + (timedelta(hours=8) if user.is_admin else timedelta(days=30)),
        )
    )
    user.last_login = now()
    db.session.commit()
    session["token"] = token
    session.permanent = True


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user.is_admin:
            abort(403)
        if g.user.must_change_password:
            return redirect(url_for("blog.password"))
        return view(*args, **kwargs)

    return wrapped


def can_view(post, user=None, at=None):
    if user and user.is_admin and not user.must_change_password:
        return True
    tiers = set(user.tier_ids) if user and not user.is_admin else set()
    if post.public or tiers.intersection(post.tier_ids):
        return True
    instant = at or now()
    return any(
        release.at <= instant
        and (release.public or tiers.intersection(release.tier_ids))
        for release in post.releases
    )
