"""Patreon API v2. OAuth bearer tokens are used transiently, never persisted."""

import requests
from flask import current_app

API = "https://www.patreon.com/api/oauth2/v2"
TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
AUTHORIZE_URL = "https://www.patreon.com/oauth2/authorize"


class PatreonError(Exception):
    pass


def request_json(method, url, **kwargs):
    headers = {"User-Agent": "PatreonMembersStudio/1.0"}
    headers.update(kwargs.pop("headers", {}))
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=(5, 20),
            allow_redirects=False,
            **kwargs,
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise PatreonError("Unexpected Patreon response.")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors"):
            raise PatreonError("Invalid Patreon response.")
        return payload
    except (requests.RequestException, ValueError) as exc:
        # Do not log request/response bodies, authorization headers, or OAuth codes.
        raise PatreonError("Patreon is unavailable or rejected the request.") from exc


def identity(code):
    config = current_app.config
    token = request_json(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config["PATREON_CLIENT_ID"],
            "client_secret": config["PATREON_CLIENT_SECRET"],
            "redirect_uri": config["SITE_URL"] + "/auth/patreon/callback",
        },
    ).get("access_token")
    if not isinstance(token, str) or not token:
        raise PatreonError("Patreon did not return an access token.")
    payload = request_json(
        "GET",
        API + "/identity",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "include": "memberships.currently_entitled_tiers,memberships.campaign",
            "fields[user]": "full_name",
            "fields[member]": "patron_status",
            "fields[tier]": "title,amount_cents",
        },
    )
    return parse_identity(payload, config["PATREON_CAMPAIGN_ID"])


def parse_identity(payload, campaign_id):
    try:
        user = payload["data"]
        if (
            user["type"] != "user"
            or not isinstance(user["id"], str)
            or not 1 <= len(user["id"]) <= 100
        ):
            raise ValueError
        memberships = user["relationships"]["memberships"]["data"]
        if not isinstance(memberships, list):
            raise ValueError
        member_ids = {m["id"] for m in memberships if m["type"] == "member"}
        seen_members = set()
        tier_ids = set()
        for item in payload.get("included", []):
            if item["type"] != "member" or item["id"] not in member_ids:
                continue
            seen_members.add(item["id"])
            relationships = item["relationships"]
            campaign = relationships["campaign"]["data"]
            if campaign["id"] != campaign_id or campaign["type"] != "campaign":
                continue
            if item["attributes"]["patron_status"] != "active_patron":
                continue
            for tier in relationships["currently_entitled_tiers"]["data"]:
                if tier["type"] != "tier" or not isinstance(tier["id"], str):
                    raise ValueError
                tier_ids.add(tier["id"])
        if seen_members != member_ids:
            raise ValueError
        if not isinstance(user.get("attributes", {}), dict):
            raise ValueError
        name = user.get("attributes", {}).get("full_name") or "Patreon member"
        if not isinstance(name, str):
            raise ValueError
        return user["id"], name[:200], sorted(tier_ids)
    except (KeyError, TypeError, ValueError) as exc:
        raise PatreonError("Patreon returned an incomplete identity.") from exc


def campaign_tiers():
    token = current_app.config["PATREON_CREATOR_ACCESS_TOKEN"]
    if not token:
        raise PatreonError("Set PATREON_CREATOR_ACCESS_TOKEN to sync campaign tiers.")
    campaign_id = current_app.config["PATREON_CAMPAIGN_ID"]
    payload = request_json(
        "GET",
        f"{API}/campaigns/{campaign_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"include": "tiers", "fields[tier]": "title,amount_cents"},
    )
    try:
        if (
            payload["data"]["id"] != campaign_id
            or payload["data"]["type"] != "campaign"
        ):
            raise ValueError
        ids = {
            entry["id"]
            for entry in payload["data"]["relationships"]["tiers"]["data"]
            if entry["type"] == "tier"
        }
        tiers = []
        for item in payload.get("included", []):
            if item["type"] == "tier" and item["id"] in ids:
                title = item["attributes"]["title"]
                amount = item["attributes"]["amount_cents"]
                if (
                    not isinstance(title, str)
                    or not isinstance(amount, int)
                    or amount < 0
                ):
                    raise ValueError
                tiers.append((item["id"], title[:200], amount))
        if {item[0] for item in tiers} != ids:
            raise ValueError
        return tiers
    except (KeyError, TypeError, ValueError) as exc:
        raise PatreonError("Patreon returned incomplete campaign tiers.") from exc
