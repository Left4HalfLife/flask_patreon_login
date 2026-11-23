from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


post_tags = db.Table(
    "post_tags",
    db.Column(
        "post_id", db.ForeignKey("post.id", ondelete="CASCADE"), primary_key=True
    ),
    db.Column("tag_id", db.ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patreon_id = db.Column(db.String(100), unique=True)
    name = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    password_hash = db.Column(db.Text)
    totp_secret = db.Column(db.String(64))
    last_totp_step = db.Column(db.Integer, default=-1, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    tier_ids = db.Column(db.JSON, default=list, nullable=False)
    last_login = db.Column(db.DateTime, default=now, nullable=False, index=True)


class LoginSession(db.Model):
    token_hash = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(
        db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    user = db.relationship(User)


class RateLimit(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    count = db.Column(db.Integer, default=1, nullable=False)
    window = db.Column(db.Integer, nullable=False, index=True)


class Tier(db.Model):
    id = db.Column(db.String(100), primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False, default=0)


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False, unique=True)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now, nullable=False)
    public = db.Column(db.Boolean, default=False, nullable=False)
    tier_ids = db.Column(db.JSON, default=list, nullable=False)
    tags = db.relationship(Tag, secondary=post_tags, lazy="selectin")
    releases = db.relationship(
        "Release", cascade="all, delete-orphan", lazy="selectin", order_by="Release.at"
    )
    attachments = db.relationship(
        "Attachment", cascade="all, delete-orphan", lazy="selectin"
    )


class Release(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.ForeignKey("post.id", ondelete="CASCADE"), nullable=False)
    at = db.Column(db.DateTime, nullable=False)
    public = db.Column(db.Boolean, default=False, nullable=False)
    tier_ids = db.Column(db.JSON, default=list, nullable=False)


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.ForeignKey("post.id", ondelete="CASCADE"), nullable=False)
    storage_name = db.Column(db.String(64), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)
    mime = db.Column(db.String(100), nullable=False)
    kind = db.Column(db.String(10), nullable=False)
    post = db.relationship(Post, back_populates="attachments")
