# Patreon Members' Studio

A Flask journal for Patreon creators: tagged posts, inline images and video, private file downloads, explicit per-tier access, and scheduled access for lower tiers or everyone. SQLite and uploads live in one persistent Docker volume. No JavaScript build step or external database is needed.

## Start locally

Requires Docker Engine with Docker Compose v2, Python 3 (only for generating secrets), and a TOTP authenticator such as Aegis, 1Password, or Google Authenticator.

```sh
python3 tools/setup_env.py
# Enroll the printed setup key in your authenticator.
# Edit .env to configure your Patreon OAuth client and campaign.
docker compose up --build -d
```

Open **http://localhost:8000/settings**, sign in with the generated admin password and a current six-digit TOTP code, and set a new password. No admin authoring or private media access is allowed before this change. Bootstrap credentials are used only when creating the first admin; changing `.env` later does not reset the database password or authenticator. After confirming your authenticator is enrolled, remove `ADMIN_PASSWORD` and `ADMIN_TOTP_SECRET` from `.env` and recreate containers to remove them from their environments.

The generated `.env` is mode `0600`; never commit it or share the authenticator URI. `.env.example` documents each setting. If you already have `.env`, the generator refuses to overwrite it. To generate secrets without host Python, run the script with a disposable Python container mounted to this directory (as your own UID/GID).

The journal is at **http://localhost:8000/**. An empty installation intentionally contains no fake users or sample content. Patreon login reports an explicit configuration error until credentials are set.

## Patreon API v2 setup

1. Register a **v2 client** in [Patreon's developer portal](https://www.patreon.com/portal/registration/register-clients).
2. Set `PATREON_CLIENT_ID`, `PATREON_CLIENT_SECRET`, and `PATREON_CAMPAIGN_ID` in `.env`. Use the campaign's numeric ID, not its URL slug.
3. Register the exact redirect URI `http://localhost:8000/auth/patreon/callback` locally, or `https://your-domain/auth/patreon/callback` in production. It must match `SITE_URL`.
4. Optionally set your creator access token in `PATREON_CREATOR_ACCESS_TOKEN` with the `campaigns` scope. In settings, **Sync tiers from Patreon** imports names, IDs, and display prices. Alternatively register your campaign's actual tier IDs manually.
5. After environment changes, run `docker compose up -d --force-recreate`.

The integration follows the [official Patreon API v2 reference](https://docs.patreon.com/) (also [OAuth](https://docs.patreon.com/#oauth) and [API v2](https://docs.patreon.com/#apiv2)). The documentation site's indexed reference was checked during implementation; direct retrieval of its hostname was unavailable in the development environment. Endpoints and token parameters were also checked against the official [Patreon Python SDK](https://github.com/Patreon/patreon-python) ([API](https://github.com/Patreon/patreon-python/blob/master/patreon/api.py), [OAuth](https://github.com/Patreon/patreon-python/blob/master/patreon/oauth.py)), and the nested membership include against [Patreon's WordPress v2 client](https://github.com/Patreon/patreon-wordpress/blob/master/classes/patreon_api_v2.php). This app uses a small `requests` adapter rather than the SDK, to enforce explicit timeouts, disabled redirects, HTTP status validation, and strict response parsing. The integration uses:

| Purpose | Contract |
| --- | --- |
| Authorization | `GET https://www.patreon.com/oauth2/authorize`, authorization code flow, scopes `identity identity.memberships`, random single-use state, 10-minute state expiry |
| Token exchange | `POST https://www.patreon.com/api/oauth2/token`, `grant_type=authorization_code`, matching `redirect_uri`, client ID and secret |
| Verify every login | `GET https://www.patreon.com/api/oauth2/v2/identity`, `include=memberships.currently_entitled_tiers,memberships.campaign`, `fields[user]=full_name`, `fields[member]=patron_status`, `fields[tier]=title,amount_cents` |
| Tier synchronization | `GET https://www.patreon.com/api/oauth2/v2/campaigns/{campaign_id}?include=tiers&fields[tier]=title,amount_cents` using the creator token |

`identity.memberships` can return memberships for **other creators** too. Access uses only membership IDs linked to the returned user, with the **configured campaign ID**, `patron_status=active_patron`, and the membership's exact `currently_entitled_tiers` IDs. Pledge amounts, lifetime payments, email, and tiers belonging to another campaign never grant access. Declined/former patrons receive no paid tiers. Users with no qualifying membership may still log in as free users.

On every successful login, the local account is created if absent, entitlement IDs are replaced (including clearing them on downgrade), and previous sessions are revoked. OAuth access/refresh tokens are not stored. Failed verification does not create an account or grant access; a valid re-login callback also revokes the browser's previous session before verification. Patreon HTTP errors, malformed payloads, and timeouts surface as errors, not successful logins.

**Operational limitation:** these are login-time entitlement snapshots. There are no webhooks or background refresh tokens. A cancellation or downgrade between logins can retain access until the next login or the **fixed 30-day** session expiry. For stricter revocation, add verified Patreon webhooks or shorten the member session lifetime in code. Monthly expiry means 30 days, not the next calendar month. Automated tests mock API responses; a real campaign OAuth round trip still requires your credentials.

## Publishing

- Create tags in settings, then create a post with text and optional attachments. Post bodies are plain text and are autoescaped, not executable HTML.
- Check **every tier** permitted to see the post. Higher-priced tiers do not automatically inherit access. An unchecked audience is admin-only.
- Add any number of timed access grants (up to 20). Choose lower tiers or **Free / public** and a UTC date. **+7 / +30 / +90 days** adds to the chosen date, or now if blank. Saving past dates grants access immediately.
- Grants are **additive**: an existing audience keeps access. A post can reach a lower tier after 30 days and everyone after 90 days. Dates take effect at request time, including attachments; no release scheduler needs to fire.
- Free/public means **any visitor**, without login. To publish only to a Patreon tier, select its tier ID rather than public.
- Edit or delete posts and attachments from the post editor. No public preview, title, thumbnail, body, or file is exposed for inaccessible posts; direct URLs return 404. Tag names themselves are public navigation metadata.

Supported uploads: JPEG, PNG, GIF, WebP, MP4, WebM, PDF, TXT, ZIP, 7z, MP3, WAV. Images are verified with Pillow; videos receive container-signature validation (not full decoding). Images/video display inline. Everything else downloads as an attachment, with `nosniff`. HTML and SVG uploads are disallowed. Maximum 20 attachments per post and 256 MiB per request. No archive is extracted or attachment executed. This is **not antivirus scanning**; only the trusted admin can upload, and downloads should still be treated as untrusted.

## Deployment and security

- **Use HTTPS in production.** Set `SITE_URL=https://your-domain` and `COOKIE_SECURE=true` before starting behind a TLS reverse proxy. Cookies are HttpOnly and SameSite=Lax; HSTS is sent with secure cookies. HTTP plus `COOKIE_SECURE=false` is a local-development exception.
- Compose binds only `127.0.0.1:8000`. Keep the app port private behind your reverse proxy; do not expose Gunicorn directly to the Internet. Proxy to the loopback binding and preserve the original `Host`, or explicitly set it to `SITE_URL`'s hostname. Untrusted Host headers are rejected. Set proxy upload/time limits for 256 MiB uploads.
- TLS proxy configuration and certificate issuance depend on your domain and are not bundled. Do not serve the data volume as static files. Do not enable proxy/CDN caching for dynamic pages or `/media/`; all responses have `private, no-store`.
- Database-backed random session tokens are stored **hashed**. Member sessions expire 30 days after issuance without sliding renewal; admin sessions expire in 8 hours. Logout revokes the token. All mutation forms, including login/logout, use CSRF protection.
- Admin passwords use Werkzeug's scrypt hashing. TOTP codes are accepted for the current 30-second step only and cannot be replayed. Keep host/authenticator clocks synchronized. Password changes revoke all admin sessions.
- Login/password attempts are limited to 5 per 15-minute window per peer IP using atomic SQLite counters; OAuth endpoints to 30. Forwarded-IP headers are deliberately not trusted, so requests through one reverse proxy may share a bucket. Configure additional edge throttling for Internet deployments.
- CSP disallows inline scripts, external resources, framing, and plugins. Templates autoescape text. File names are sanitized, storage names are random, and every media request checks current access before serving (including video range requests).
- Containers run as UID/GID 10001, with a read-only root filesystem, dropped capabilities, and a private persistent data directory. Restrict access to `.env`, Docker, host backups, and the volume: the SQLite database contains password hashes, the admin's TOTP secret, and membership IDs. Encryption at rest is a host/storage responsibility.
- The signed browser cookie contains only opaque login/state/CSRF material. Neither Patreon tokens nor the TOTP secret is sent to clients. Application errors do not log OAuth tokens, codes, or API response bodies. Avoid enabling proxy access logging of OAuth callback query strings.
- Losing the admin password or TOTP requires an **operator-controlled, offline recovery** of the admin record from a trusted backup/database maintenance procedure; there is deliberately no unauthenticated reset endpoint or environment-variable backdoor.

## Maintenance and data

The `maintenance` Compose service runs cleanup hourly, deleting **non-admin accounts whose last successful login is over 90 days old**, their login sessions via SQLite foreign-key cascades, expired sessions, and old rate-limit counters. Reading a post does not extend `last_login`. A deleted user's next Patreon login recreates their account and verifies current entitlements. Posts and admin accounts are never removed by cleanup.

```sh
docker compose ps
docker compose logs --tail=100 web maintenance
docker compose exec web flask cleanup         # optional immediate cleanup
docker compose down                          # preserves the volume
```

Back up **both** `/data/blog.sqlite3` and `/data/uploads` consistently. Stop web and maintenance before copying the named volume, or use SQLite's backup API with coordinated upload backups. SQLite uses WAL and a 10-second busy timeout; copying just the live `.sqlite3` file can lose committed WAL data. Restore permissions for UID 10001. `docker compose down -v` permanently deletes the data. The initial schema is created automatically; future schema changes will require an explicit migration plan rather than silently modifying tables.

## Local development and checks

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
python3 tools/setup_env.py                    # once; do not overwrite existing .env
.venv/bin/flask --app patron_blog init-db
.venv/bin/flask --app patron_blog run --port 8000
.venv/bin/pytest -q
.venv/bin/ruff check patron_blog tests tools
```

Outside Docker the data defaults to Flask's `instance/` directory; set `DATA_DIR` to use another directory. Run `flask --app patron_blog cleanup` hourly with your service manager when not using Compose. Do not run Flask's development server in production.
