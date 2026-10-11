"""Platform API, week 1: organisations and events.

GET  /me                              -> the signed-in user and the orgs they belong to
POST /orgs {name}                     -> create an organisation; caller becomes owner
GET  /orgs/{orgId}/events             -> list the org's events
POST /orgs/{orgId}/events {name, startDate, endDate?, slug?} -> create an event
GET  /orgs/{orgId}/events/{eventId}   -> one event, with guest and upload links
GET  /e/{slug}                        -> public event info for the guest page (no sign-in)
POST /e/{slug}/match {code, image}    -> guest selfie: match faces, start a session, return the gallery
GET  /e/{slug}/gallery (Bearer)       -> returning guest's gallery
POST /e/{slug}/upload-urls {code, visibility, uploader, files} -> photographer upload slots (code = upload key)
POST /e/{slug}/upload-guide {code}    -> sorting rules shown to the photographer

Storage layout:
  incoming/<orgId>/<eventId>/<public|private|auto>/<uploader>/<rand>__<file>   written by photographers
  events/<eventId>/<public|private>/<thumb|web|original>/<photoId>.jpg  published by the processor
Faces in private photos live in Rekognition collection ev-<eventId> with ExternalImageId = photoId.

Codes are returned once at creation and stored only as hashes.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import date, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

TABLE = os.environ["TABLE"]
BUCKET = os.environ.get("BUCKET", "")
table = boto3.resource("dynamodb").Table(TABLE)
s3 = boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
rekognition = boto3.client("rekognition")

MATCH_THRESHOLD = 95
SESSION_DAYS = 30
URL_TTL = 2 * 60 * 60
MAX_SELFIE_BYTES = 4_000_000
MAX_PHOTO_BYTES = 80 * 1024 * 1024
UPLOAD_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
DEFAULT_RULES = """PUBLIC (every guest can see):
- Venue, decor, flowers, cake, table settings and other details
- Any photo with the main couple or hosts together (applied automatically once their faces are added)
- Portraits of the couple or hosts
- Wide shots of the ceremony, entrances, first dance or speeches where guests are part of the crowd
- Large posed group photos that everyone lined up for

PRIVATE (only people in the photo can see):
- Candid close-ups of guests
- Small groups of guests, table shots, people eating or drinking
- Dancing and party candids that focus on particular guests
- Children without the couple or hosts
- Anything unflattering, awkward, or that a guest might not want shared widely
- Anything you are unsure about"""
_list_cache = {}

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
        if route == "POST /e/{slug}/match":
            return guest_match(event_for_slug(params["slug"]), body(event))
        if route == "GET /e/{slug}/gallery":
            return guest_gallery(event_for_slug(params["slug"]), event)
        if route == "POST /e/{slug}/upload-urls":
            return upload_urls(event_for_slug(params["slug"]), body(event))
        if route == "POST /e/{slug}/upload-guide":
            ev = event_for_slug(params["slug"])
            check_code(body(event).get("code"), ev.get("uploadKeyHash"), "That upload code isn't right.")
            rules = ev.get("sortingRules") or ""
            return respond(200, {"rules": rules or DEFAULT_RULES, "isDefault": not rules})

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


# ---------- Guests ----------

def event_for_slug(slug):
    if not re.fullmatch(r"[a-z0-9-]{3,48}", slug or ""):
        raise HttpError(404, "Event not found.")
    link = table.get_item(Key={"pk": f"SLUG#{slug}", "sk": "EVENT"}).get("Item")
    item = link and table.get_item(Key={"pk": f"ORG#{link['orgId']}", "sk": f"EVENT#{link['eventId']}"}).get("Item")
    if not item or item.get("status") != "active":
        raise HttpError(404, "Event not found.")
    return item


def check_code(given, stored_hash, message):
    given = str(given or "").strip()
    if not given or not stored_hash or not hmac.compare_digest(hash_secret(given), stored_hash):
        raise HttpError(403, message)


def guest_match(ev, data):
    check_code(data.get("code"), ev.get("guestCodeHash"), "That event code isn't right. Check your invitation or the card on your table.")
    image = str(data.get("image", ""))
    if "," in image[:100]:
        image = image.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(image, validate=True)
    except Exception:
        raise HttpError(400, "The selfie didn't upload correctly. Take it again.")
    if not image_bytes or len(image_bytes) > MAX_SELFIE_BYTES:
        raise HttpError(400, "The selfie is too large. Take it again.")

    event_id = ev["eventId"]
    try:
        result = rekognition.search_faces_by_image(CollectionId=f"ev-{event_id}", Image={"Bytes": image_bytes},
                                                   FaceMatchThreshold=MATCH_THRESHOLD, MaxFaces=4096, QualityFilter="AUTO")
    except rekognition.exceptions.InvalidParameterException:
        raise HttpError(422, "No face found. Face the camera in good light and try again.")
    except rekognition.exceptions.ResourceNotFoundException:
        result = {"FaceMatches": []}  # no private photos uploaded yet
    private_ids = sorted({m["Face"]["ExternalImageId"] for m in result.get("FaceMatches", []) if m["Face"].get("ExternalImageId")})

    token = secrets.token_urlsafe(32)
    now = int(time.time())
    table.put_item(Item={"pk": f"SESSION#{token}", "sk": "META", "eventId": event_id, "privateIds": private_ids,
                         "createdAt": now, "expiresAt": now + SESSION_DAYS * 86400})
    return respond(200, {"token": token, **build_gallery(event_id, private_ids)})


def guest_gallery(ev, event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    item = token and table.get_item(Key={"pk": f"SESSION#{token}", "sk": "META"}).get("Item")
    if not item or item.get("eventId") != ev["eventId"] or int(item.get("expiresAt", 0)) < time.time():
        raise HttpError(401, "Take a selfie to see your photos.")
    return respond(200, build_gallery(ev["eventId"], list(item.get("privateIds", []))))


def list_photo_ids(event_id, visibility):
    key = (event_id, visibility)
    cached = _list_cache.get(key)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    ids = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"events/{event_id}/{visibility}/thumb/"):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if name.endswith(".jpg"):
                ids.append(name[:-4])
    _list_cache[key] = (ids, time.time())
    return ids


def build_gallery(event_id, private_ids):
    public_ids = list_photo_ids(event_id, "public")
    public_set, existing = set(public_ids), set(list_photo_ids(event_id, "private"))
    private_ids = [p for p in private_ids if p not in public_set and p in existing]
    photos = [photo_entry(event_id, "public", p) for p in public_ids] + [photo_entry(event_id, "private", p) for p in private_ids]
    photos.sort(key=lambda p: p["id"])
    return {"photos": photos, "yourPhotoCount": len(private_ids), "publicCount": len(public_ids)}


def photo_entry(event_id, visibility, pid):
    def url(size, download=False):
        params = {"Bucket": BUCKET, "Key": f"events/{event_id}/{visibility}/{size}/{pid}.jpg"}
        if download:
            params["ResponseContentDisposition"] = f'attachment; filename="{pid}.jpg"'
        return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=URL_TTL)
    return {"id": pid, "yours": visibility == "private", "thumb": url("thumb"), "web": url("web"), "download": url("original", True)}


# ---------- Photographer uploads ----------

def upload_urls(ev, data):
    check_code(data.get("code"), ev.get("uploadKeyHash"), "That upload code isn't right.")
    visibility = data.get("visibility")
    if visibility not in ("public", "private", "auto"):
        raise HttpError(400, "Choose public, private, or auto.")
    uploader = re.sub(r"[^a-z0-9-]", "-", str(data.get("uploader", "photographer")).lower()).strip("-")[:40] or "photographer"
    files = data.get("files") or []
    if not isinstance(files, list) or not 1 <= len(files) <= 50:
        raise HttpError(400, "Send between 1 and 50 files per request.")
    uploads = []
    for f in files:
        name = str((f or {}).get("name", ""))
        ext = os.path.splitext(name)[1].lower()
        if ext not in UPLOAD_TYPES:
            uploads.append({"name": name, "error": "Only JPG and PNG files are accepted."})
            continue
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(name)[0])[:80] or "photo"
        key = f"incoming/{ev['pk'][4:]}/{ev['eventId']}/{visibility}/{uploader}/{secrets.token_hex(4)}__{stem}{ext}"
        ctype = UPLOAD_TYPES[ext]
        post = s3.generate_presigned_post(Bucket=BUCKET, Key=key, Fields={"Content-Type": ctype},
                                          Conditions=[{"Content-Type": ctype}, ["content-length-range", 1, MAX_PHOTO_BYTES]],
                                          ExpiresIn=3600)
        uploads.append({"name": name, "url": post["url"], "fields": post["fields"]})
    return respond(200, {"uploads": uploads})


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
