"""Platform API, week 1: organisations and events.

GET  /me                              -> the signed-in user and the orgs they belong to
POST /orgs {name}                     -> create an organisation; caller becomes owner
GET  /orgs/{orgId}/events             -> list the org's events
POST /orgs/{orgId}/events {name, startDate, endDate?, slug?} -> create an event
GET  /orgs/{orgId}/events/{eventId}   -> one event, with guest and upload links
GET  /e/{slug}                        -> public event info for the guest page (no sign-in)

Codes are returned once at creation and stored only as hashes.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import date, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE = os.environ["TABLE"]
table = boto3.resource("dynamodb").Table(TABLE)

DEFAULT_RETENTION_DAYS = 60
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L lookalikes


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handler(event, _context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    try:
        if route == "GET /e/{slug}":
            return public_event(params["slug"])

        user = current_user(event)
        if route == "GET /me":
            return respond(200, {"user": user, "orgs": orgs_for(user["id"])})
        if route == "POST /orgs":
            return create_org(user, body(event))

        org_id = params.get("orgId", "")
        require_member(user["id"], org_id)
        if route == "GET /orgs/{orgId}/events":
            return list_events(org_id)
        if route == "POST /orgs/{orgId}/events":
            return create_event(org_id, body(event))
        if route == "GET /orgs/{orgId}/events/{eventId}":
            return get_event(org_id, params["eventId"])
        return respond(404, {"error": "Not found."})
    except HttpError as err:
        return respond(err.status, {"error": str(err)})


# ---------- Identity ----------

def current_user(event):
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    if not claims.get("sub"):
        raise HttpError(401, "Sign in first.")
    return {"id": claims["sub"], "email": claims.get("email", "")}


def orgs_for(user_id):
    items = table.query(KeyConditionExpression=Key("pk").eq(f"USER#{user_id}"))["Items"]
    out = []
    for item in items:
        org_id = item["sk"].split("#", 1)[1]
        meta = table.get_item(Key={"pk": f"ORG#{org_id}", "sk": "META"}).get("Item") or {}
        out.append({"id": org_id, "name": meta.get("name", ""), "plan": meta.get("plan", "trial"), "role": item.get("role")})
    return out


def require_member(user_id, org_id):
    if not re.fullmatch(r"[a-z0-9]{12}", org_id or ""):
        raise HttpError(404, "Not found.")
    member = table.get_item(Key={"pk": f"ORG#{org_id}", "sk": f"USER#{user_id}"}).get("Item")
    if not member:
        raise HttpError(404, "Not found.")  # don't reveal whether the org exists
    return member


# ---------- Organisations ----------

def create_org(user, data):
    name = clean_text(data.get("name"), 80)
    if not name:
        raise HttpError(400, "Give the organisation a name.")
    org_id = new_id()
    now = int(time.time())
    with table.batch_writer() as batch:
        batch.put_item(Item={"pk": f"ORG#{org_id}", "sk": "META", "name": name, "plan": "trial",
                             "createdAt": now, "createdBy": user["id"]})
        batch.put_item(Item={"pk": f"ORG#{org_id}", "sk": f"USER#{user['id']}", "role": "owner",
                             "email": user["email"], "createdAt": now})
        batch.put_item(Item={"pk": f"USER#{user['id']}", "sk": f"ORG#{org_id}", "role": "owner"})
    return respond(201, {"id": org_id, "name": name, "plan": "trial", "role": "owner"})


# ---------- Events ----------

def list_events(org_id):
    items = table.query(KeyConditionExpression=Key("pk").eq(f"ORG#{org_id}") & Key("sk").begins_with("EVENT#"))["Items"]
    events = [event_view(i) for i in items]
    events.sort(key=lambda e: e["startDate"], reverse=True)
    return respond(200, {"events": events})


def create_event(org_id, data):
    name = clean_text(data.get("name"), 100)
    start = parse_date(data.get("startDate"), "startDate")
    end = parse_date(data.get("endDate"), "endDate") if data.get("endDate") else start
    if not name:
        raise HttpError(400, "Give the event a name.")
    if end < start:
        raise HttpError(400, "The end date is before the start date.")

    event_id = new_id()
    slug = make_slug(data.get("slug") or name)
    guest_code = new_code(8)
    upload_key = secrets.token_urlsafe(18)
    delete_after = (end + timedelta(days=DEFAULT_RETENTION_DAYS)).isoformat()

    try:
        # Claim the slug first; fails if another event already uses it.
        table.put_item(Item={"pk": f"SLUG#{slug}", "sk": "EVENT", "orgId": org_id, "eventId": event_id},
                       ConditionExpression="attribute_not_exists(pk)")
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HttpError(409, "That guest link is taken. Choose another.")
        raise

    item = {
        "pk": f"ORG#{org_id}", "sk": f"EVENT#{event_id}", "eventId": event_id, "name": name, "slug": slug,
        "startDate": start.isoformat(), "endDate": end.isoformat(), "deleteAfter": delete_after,
        "status": "active", "guestCodeHash": hash_secret(guest_code), "uploadKeyHash": hash_secret(upload_key),
        "createdAt": int(time.time()),
    }
    table.put_item(Item=item)
    view = event_view(item)
    view["guestCode"] = guest_code      # shown once; only the hash is stored
    view["uploadKey"] = upload_key
    return respond(201, view)


def get_event(org_id, event_id):
    if not re.fullmatch(r"[a-z0-9]{12}", event_id or ""):
        raise HttpError(404, "Not found.")
    item = table.get_item(Key={"pk": f"ORG#{org_id}", "sk": f"EVENT#{event_id}"}).get("Item")
    if not item:
        raise HttpError(404, "Not found.")
    return respond(200, event_view(item))


def public_event(slug):
    if not re.fullmatch(r"[a-z0-9-]{3,48}", slug or ""):
        raise HttpError(404, "Event not found.")
    link = table.get_item(Key={"pk": f"SLUG#{slug}", "sk": "EVENT"}).get("Item")
    if not link:
        raise HttpError(404, "Event not found.")
    item = table.get_item(Key={"pk": f"ORG#{link['orgId']}", "sk": f"EVENT#{link['eventId']}"}).get("Item") or {}
    org = table.get_item(Key={"pk": f"ORG#{link['orgId']}", "sk": "META"}).get("Item") or {}
    if item.get("status") != "active":
        raise HttpError(404, "Event not found.")
    return respond(200, {
        "name": item["name"], "startDate": item["startDate"], "endDate": item["endDate"],
        "photosBy": org.get("name", ""), "branding": org.get("branding", {}),
    })


def event_view(item):
    return {k: item[k] for k in ("eventId", "name", "slug", "startDate", "endDate", "deleteAfter", "status", "createdAt") if k in item}


# ---------- Helpers ----------

def new_id():
    return secrets.token_hex(6)  # 12 lowercase hex characters


def new_code(length):
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def hash_secret(value):
    # Codes are random and high-entropy, so a fast hash with a fixed pepper is enough here.
    return hashlib.sha256(f"eventphotos:{value.strip().upper()}".encode()).hexdigest()


def make_slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "event"
    if len(slug) < 3:
        slug = f"{slug}-event"
    return f"{slug}-{secrets.token_hex(2)}"  # short suffix keeps common names like "ade-and-tola" unique


def parse_date(value, field):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise HttpError(400, f"{field} must be a date like 2026-11-19.")


def clean_text(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HttpError(400, "Request body must be JSON.")
    if not isinstance(data, dict):
        raise HttpError(400, "Request body must be a JSON object.")
    return data


def respond(status, payload):
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(payload, default=str)}
