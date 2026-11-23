import hmac
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import pyotp
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from . import patreon
from .auth import (
    admin_required,
    can_view,
    load_user,
    rate_limit,
    revoke_session,
    sign_in,
)
from .models import Attachment, LoginSession, Post, Release, Tag, Tier, User, db, now
from .uploads import remove_files, save_upload, validate_upload

bp = Blueprint("blog", __name__)
bp.before_app_request(load_user)


def tiers():
    return db.session.scalars(
        db.select(Tier).order_by(Tier.amount_cents, Tier.title)
    ).all()


def required_text(field, maximum):
    value = request.form.get(field, "").strip()
    if not value or len(value) > maximum:
        abort(
            400,
            f"{field.replace('_', ' ').capitalize()} must be 1-{maximum} characters.",
        )
    return value


@bp.get("/health")
def health():
    db.session.execute(db.text("SELECT 1"))
    return {"status": "ok"}


@bp.get("/")
def index():
    query = db.select(Post).order_by(Post.created_at.desc(), Post.id.desc())
    allowed_tiers = g.user.tier_ids if g.user and not g.user.is_admin else []
    if not (g.user and g.user.is_admin and not g.user.must_change_password):
        release_allowed = Release.public.is_(True)
        # JSON membership must use exact elements, never substring matching.
        post_tiers = db.func.json_each(Post.tier_ids).table_valued("value").alias("pt")
        release_tiers = (
            db.func.json_each(Release.tier_ids).table_valued("value").alias("rt")
        )
        if allowed_tiers:
            release_allowed = or_(
                release_allowed,
                db.select(1)
                .select_from(release_tiers)
                .where(release_tiers.c.value.in_(allowed_tiers))
                .exists(),
            )
        query = query.where(
            or_(
                Post.public.is_(True),
                db.select(1)
                .select_from(post_tiers)
                .where(post_tiers.c.value.in_(allowed_tiers))
                .exists(),
                Post.releases.any(db.and_(Release.at <= now(), release_allowed)),
            )
        )
    selected_tag = request.args.get("tag", "").strip()
    if selected_tag:
        query = query.where(Post.tags.any(Tag.name == selected_tag))
    page = db.paginate(query, per_page=12, max_per_page=12, error_out=False)
    return render_template(
        "index.html",
        page=page,
        tags=db.session.scalars(db.select(Tag).order_by(Tag.name)).all(),
        selected_tag=selected_tag,
    )


@bp.get("/posts/<int:post_id>")
def post(post_id):
    entry = db.get_or_404(Post, post_id)
    if not can_view(entry, g.user):
        abort(404)
    return render_template("post.html", post=entry)


@bp.get("/media/<int:attachment_id>")
def media(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    if not can_view(attachment.post, g.user):
        abort(404)
    path = Path(current_app.config["UPLOAD_FOLDER"]) / attachment.storage_name
    return send_file(
        path,
        mimetype=attachment.mime,
        download_name=attachment.name,
        as_attachment=attachment.kind == "file",
        conditional=True,
        etag=False,
        max_age=0,
    )


@bp.post("/logout")
def logout():
    revoke_session()
    db.session.commit()
    return redirect(url_for("blog.index"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        rate_limit("admin-login", 5)
        name = request.form.get("username", "")[:200]
        password = request.form.get("password", "")
        code = request.form.get("totp", "")
        admin = db.session.scalar(
            db.select(User).where(User.is_admin.is_(True), User.name == name)
        )
        valid_password = check_password_hash(
            admin.password_hash if admin else current_app.config["DUMMY_PASSWORD_HASH"],
            password,
        )
        step = int(time.time()) // 30
        if not (
            admin
            and valid_password
            and pyotp.TOTP(admin.totp_secret).verify(
                code, for_time=step * 30, valid_window=0
            )
        ):
            abort(400, "Invalid administrator credentials or authenticator code.")
        changed = db.session.execute(
            db.update(User)
            .where(User.id == admin.id, User.last_totp_step < step)
            .values(last_totp_step=step)
        ).rowcount
        if not changed:
            db.session.rollback()
            abort(
                400,
                "That authenticator code has already been used. Wait for a new code.",
            )
        sign_in(admin)
        return redirect(url_for("blog.settings"))
    if not g.user or not g.user.is_admin:
        return render_template("login.html")
    if g.user.must_change_password:
        return redirect(url_for("blog.password"))
    return render_template(
        "settings.html",
        tiers=tiers(),
        tags=db.session.scalars(db.select(Tag).order_by(Tag.name)).all(),
    )


@bp.route("/settings/password", methods=["GET", "POST"])
def password():
    if not g.user or not g.user.is_admin:
        abort(403)
    if request.method == "POST":
        rate_limit("password", 5)
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(g.user.password_hash, current):
            abort(400, "Incorrect current password.")
        if (
            len(new) < 14
            or len(new) > 256
            or new != confirm
            or check_password_hash(g.user.password_hash, new)
        ):
            abort(
                400,
                "Use a different 14-256 character password and matching confirmation.",
            )
        g.user.password_hash = generate_password_hash(new)
        g.user.must_change_password = False
        db.session.execute(
            db.delete(LoginSession).where(LoginSession.user_id == g.user.id)
        )
        sign_in(g.user)
        flash(
            "Password changed. All previous administrator sessions were revoked.",
            "success",
        )
        return redirect(url_for("blog.settings"))
    return render_template("password.html")


@bp.post("/auth/patreon")
def patreon_login():
    rate_limit("oauth-start", 30)
    config = current_app.config
    if not all(
        config[key]
        for key in ("PATREON_CLIENT_ID", "PATREON_CLIENT_SECRET", "PATREON_CAMPAIGN_ID")
    ):
        abort(503, "Patreon login is not configured. Contact the site administrator.")
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_started"] = time.time()
    params = {
        "response_type": "code",
        "client_id": config["PATREON_CLIENT_ID"],
        "redirect_uri": config["SITE_URL"] + "/auth/patreon/callback",
        "scope": "identity identity.memberships",
        "state": state,
    }
    return redirect(patreon.AUTHORIZE_URL + "?" + urlencode(params))


@bp.get("/auth/patreon/callback")
def patreon_callback():
    rate_limit("oauth-callback", 30)
    expected = session.pop("oauth_state", None)
    started = session.pop("oauth_started", 0)
    state = request.args.get("state", "")
    if (
        not expected
        or not state.isascii()
        or not hmac.compare_digest(expected, state)
        or not 0 <= time.time() - started <= 600
    ):
        abort(400, "OAuth session is invalid or expired. Please start login again.")
    # A failed re-login must not leave the previous cached entitlements usable.
    revoke_session()
    db.session.commit()
    if request.args.get("error"):
        abort(400, "Patreon authorization was declined. Please start login again.")
    code = request.args.get("code", "")
    if not code or len(code) > 2048:
        abort(400, "Missing or invalid authorization code.")
    try:
        patreon_id, name, entitled_tiers = patreon.identity(code)
    except patreon.PatreonError as exc:
        current_app.logger.warning("Patreon identity verification failed.")
        abort(502, str(exc))
    user = db.session.scalar(db.select(User).where(User.patreon_id == patreon_id))
    if user is None:
        user = User(patreon_id=patreon_id, name=name)
        db.session.add(user)
    user.name = name
    user.tier_ids = entitled_tiers
    # Re-login invalidates all older sessions, including their cached tier snapshots.
    if user.id is not None:
        db.session.execute(
            db.delete(LoginSession).where(LoginSession.user_id == user.id)
        )
    sign_in(user)
    return redirect(url_for("blog.index"))


@bp.post("/settings/tiers/sync")
@admin_required
def sync_tiers():
    try:
        result = patreon.campaign_tiers()
    except patreon.PatreonError as exc:
        current_app.logger.warning("Patreon tier synchronization failed.")
        abort(502, str(exc))
    for tier_id, title, amount in result:
        tier = db.session.get(Tier, tier_id)
        if tier is None:
            tier = Tier(id=tier_id)
            db.session.add(tier)
        tier.title, tier.amount_cents = title, amount
    db.session.commit()
    flash(
        "Campaign tiers synchronized. Existing access rules were preserved.", "success"
    )
    return redirect(url_for("blog.settings"))


@bp.post("/settings/tiers")
@admin_required
def add_tier():
    tier_id = required_text("tier_id", 100)
    if not tier_id.isascii() or not tier_id.isdecimal():
        abort(400, "Use the numeric Patreon tier ID, not a local rank.")
    if db.session.get(Tier, tier_id):
        abort(400, "That Patreon tier is already registered.")
    title = required_text("title", 200)
    amount = request.form.get("amount_cents", "0")
    if not amount.isdecimal() or len(amount) > 9:
        abort(400, "Amount must be a nonnegative integer in cents.")
    db.session.add(Tier(id=tier_id, title=title, amount_cents=int(amount)))
    db.session.commit()
    flash("Tier registered. Only IDs returned by Patreon grant access.", "success")
    return redirect(url_for("blog.settings"))


@bp.post("/settings/tags")
@admin_required
def add_tag():
    name = required_text("name", 60)
    db.session.add(Tag(name=name))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(400, "That tag already exists.")
    flash("Tag created.", "success")
    return redirect(url_for("blog.settings"))


def selected_tiers(field):
    selected = sorted(set(request.form.getlist(field)))
    known = set(db.session.scalars(db.select(Tier.id)).all())
    if not set(selected).issubset(known):
        abort(400, "Unknown tier selected.")
    return selected


@bp.route("/settings/posts/new", methods=["GET", "POST"])
@bp.route("/settings/posts/<int:post_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_post(post_id=None):
    entry = (
        db.get_or_404(Post, post_id) if post_id is not None else Post(title="", body="")
    )
    if request.method == "POST":
        title = required_text("title", 200)
        body = request.form.get("body", "").strip()
        if len(body) > 100_000:
            abort(400, "Post body is too long.")
        selected = selected_tiers("tier_ids")
        tag_ids = request.form.getlist("tags")
        if any(not value.isdecimal() for value in tag_ids):
            abort(400, "Invalid tag selected.")
        chosen_tags = db.session.scalars(
            db.select(Tag).where(Tag.id.in_([int(value) for value in tag_ids]))
        ).all()
        if len(chosen_tags) != len(set(tag_ids)):
            abort(400, "Unknown tag selected.")
        dates = request.form.getlist("release_at")
        if len(dates) > 20:
            abort(400, "Use at most 20 timed releases per post.")
        releases = []
        for index, value in enumerate(dates):
            released_tiers = selected_tiers(f"release_tiers_{index}")
            public = request.form.get(f"release_public_{index}") == "on"
            if not value and not released_tiers and not public:
                continue
            try:
                at = datetime.strptime(value, "%Y-%m-%dT%H:%M")
            except ValueError:
                abort(400, "Every release needs a valid UTC date and time.")
            if not public and not released_tiers:
                abort(400, "Select a tier or public access for each timed release.")
            releases.append(Release(at=at, tier_ids=released_tiers, public=public))
        files = [
            upload for upload in request.files.getlist("attachments") if upload.filename
        ]
        if len(files) + len(entry.attachments) > 20:
            abort(400, "Use at most 20 attachments per post.")
        try:
            validated = [validate_upload(upload) for upload in files]
        except ValueError as exc:
            abort(400, str(exc))
        entry.title, entry.body = title, body
        entry.public = request.form.get("public") == "on"
        entry.tier_ids, entry.tags, entry.releases = selected, chosen_tags, releases
        saved = []
        try:
            for upload, metadata in zip(files, validated):
                attachment = save_upload(upload, metadata)
                saved.append(attachment)
                entry.attachments.append(attachment)
            db.session.add(entry)
            db.session.commit()
        except (OSError, SQLAlchemyError):
            # Filesystem writes are outside the DB transaction; undo only new files.
            db.session.rollback()
            remove_files(saved)
            raise
        flash("Post saved.", "success")
        return redirect(url_for("blog.post", post_id=entry.id))
    return render_template(
        "editor.html",
        post=entry,
        tiers=tiers(),
        tags=db.session.scalars(db.select(Tag).order_by(Tag.name)).all(),
    )


@bp.post("/settings/posts/<int:post_id>/delete")
@admin_required
def delete_post(post_id):
    entry = db.get_or_404(Post, post_id)
    attachments = list(entry.attachments)
    db.session.delete(entry)
    db.session.commit()
    remove_files(attachments)
    flash("Post deleted.", "success")
    return redirect(url_for("blog.index"))


@bp.post("/settings/media/<int:attachment_id>/delete")
@admin_required
def delete_attachment(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    post_id = attachment.post_id
    db.session.delete(attachment)
    db.session.commit()
    remove_files([attachment])
    flash("Attachment deleted.", "success")
    return redirect(url_for("blog.edit_post", post_id=post_id))
