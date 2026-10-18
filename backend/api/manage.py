"""Signed-in photographer tools for one event, plus studio branding.

GET  /orgs/{orgId}/events/{eventId}/photos        every photo with how it was sorted
GET  /orgs/{orgId}/events/{eventId}/rules         sorting rules (event's own or the default)
GET  /orgs/{orgId}/events/{eventId}/hosts         reference photos per host (couple, birthday person...)
GET  /orgs/{orgId}/events/{eventId}/stats         selfies, matches, downloads, photo counts
POST /orgs/{orgId}/events/{eventId}/visibility    {id, visibility}  move a photo public <-> private
POST /orgs/{orgId}/events/{eventId}/delete-photos {ids}
POST /orgs/{orgId}/events/{eventId}/resort        {ids}  re-sort with AI (runs in the processor)
POST /orgs/{orgId}/events/{eventId}/rules         {rules}  save ("" = back to default)
POST /orgs/{orgId}/events/{eventId}/hosts         {name, image}  add a reference photo
POST /orgs/{orgId}/events/{eventId}/hosts-clear   {name}
POST /orgs/{orgId}/events/{eventId}/delete-all    {confirm, includeHosts}
GET|POST /orgs/{orgId}/events/{eventId}/details    the event page guests see (hosts' names, venue, schedule...)
GET|POST /orgs/{orgId}/events/{eventId}/settings   {live, requireApproval, aiSorting*, storageMode*, hiresDays*}  (* studio only)
POST /orgs/{orgId}/events/{eventId}/visibility-bulk {ids, visibility}  share or hide up to 100 photos at once
POST /orgs/{orgId}/events/{eventId}/approve       {ids}  release photos held for approval
GET  /orgs/{orgId}/events/{eventId}/owners        event owners (the host) with dashboard access
POST /orgs/{orgId}/events/{eventId}/owners-add    {email}
POST /orgs/{orgId}/events/{eventId}/owners-remove {email}

Photos held for approval live under events/<eventId>/held/..., with the visibility they'll get in
the record's "intended" field. Guests never see the held prefix.
GET|PUT /orgs/{orgId}/branding, POST /orgs/{orgId}/branding-logo
"""
import base64
import json
import os
import re
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

PROCESSOR_FUNCTION = os.environ.get("PROCESSOR_FUNCTION", "")
DELETE_PHRASE = "DELETE ALL PHOTOS"
SIZES = ("original", "web", "thumb")
VISIBILITIES = ("public", "private", "held")
STORAGE_MODES = ("full", "window")   # full: keep originals for the event's whole life; window: hi-res for a few days only
HIRES_DAYS = (7, 14, 30)             # must match the lifecycle rules in template.yaml
LIVE_MATCH_THRESHOLD = 95
DETAIL_FIELDS = {"hosts": 120, "venue": 200, "welcome": 600, "schedule": 2000, "dressCode": 200}
BRANDING_FIELDS = {"name": 80, "tagline": 140, "link": 300, "instagram": 60, "contact": 120}

lambda_client = boto3.client("lambda")


class Ctx:
    """Shared clients and helpers handed over from app.py, so both modules use one set."""
    def __init__(self, table, s3, rekognition, bucket, http_error, respond, default_rules, list_cache, url_ttl):
        self.table, self.s3, self.rek, self.bucket = table, s3, rekognition, bucket
        self.Err, self.respond, self.default_rules = http_error, respond, default_rules
        self.list_cache, self.url_ttl = list_cache, url_ttl


def handle_event_action(c, method, org_id, event_id, action, data, role="team"):
    ev = load_event(c, org_id, event_id)
    who = "the host" if role == "owner" else "you"
    routes = {
        ("GET", "details"): lambda: c.respond(200, details_view(ev)),
        ("POST", "details"): lambda: save_details(c, ev, data),
        ("GET", "settings"): lambda: c.respond(200, settings_view(ev)),
        ("POST", "settings"): lambda: save_settings(c, ev, data, role),
        ("POST", "visibility-bulk"): lambda: move_many(c, ev, data, who),
        ("POST", "approve"): lambda: approve(c, ev, data, who),
        ("GET", "owners"): lambda: list_owners(c, ev),
        ("POST", "owners-add"): lambda: add_owner(c, org_id, ev, data),
        ("POST", "owners-remove"): lambda: remove_owner(c, ev, data),
        ("GET", "photos"): lambda: photos(c, ev),
        ("GET", "rules"): lambda: get_rules(c, ev),
        ("GET", "hosts"): lambda: host_counts(c, ev),
        ("GET", "stats"): lambda: stats(c, ev),
        ("POST", "visibility"): lambda: move(c, ev, data, who),
        ("POST", "delete-photos"): lambda: delete_photos(c, ev, data),
        ("POST", "resort"): lambda: resort(c, org_id, ev, data),
        ("POST", "rules"): lambda: save_rules(c, ev, data),
        ("POST", "hosts"): lambda: add_host(c, ev, data),
        ("POST", "hosts-clear"): lambda: clear_host(c, ev, data),
        ("POST", "delete-all"): lambda: delete_all(c, org_id, ev, data),
    }
    fn = routes.get((method, action))
    if not fn:
        raise c.Err(404, "Not found.")
    return fn()


def load_event(c, org_id, event_id):
    if not re.fullmatch(r"[a-z0-9]{12}", event_id or ""):
        raise c.Err(404, "Not found.")
    item = c.table.get_item(Key={"pk": f"ORG#{org_id}", "sk": f"EVENT#{event_id}"}).get("Item")
    if not item:
        raise c.Err(404, "Not found.")
    return item


# ---------- Photos ----------

def list_ids(c, event_id, visibility):
    ids = []
    for page in c.s3.get_paginator("list_objects_v2").paginate(Bucket=c.bucket, Prefix=f"events/{event_id}/{visibility}/thumb/"):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if name.endswith(".jpg"):
                ids.append(name[:-4])
    return ids


def records(c, event_id):
    out, kwargs = {}, {"KeyConditionExpression": Key("pk").eq(f"EVENT#{event_id}") & Key("sk").begins_with("PHOTO#")}
    while True:
        page = c.table.query(**kwargs)
        for item in page.get("Items", []):
            out[item["photoId"]] = item
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def presign(c, key):
    return c.s3.generate_presigned_url("get_object", Params={"Bucket": c.bucket, "Key": key}, ExpiresIn=c.url_ttl)


def photos(c, ev):
    eid = ev["eventId"]
    recs = records(c, eid)
    items = []
    for vis in VISIBILITIES:
        for pid in list_ids(c, eid, vis):
            r = recs.get(pid, {})
            items.append({
                "id": pid, "visibility": vis, "intended": r.get("intended", "private") if vis == "held" else vis,
                "source": r.get("source", "manual"), "reason": r.get("reason", ""),
                "confidence": float(r.get("confidence", 1)), "uploader": r.get("uploader", ""),
                "faces": len(r.get("faceIds", [])), "createdAt": int(r.get("createdAt", 0)),
                "thumb": presign(c, f"events/{eid}/{vis}/thumb/{pid}.jpg"),
                "web": presign(c, f"events/{eid}/{vis}/web/{pid}.jpg"),
            })
    items.sort(key=lambda i: (-i["createdAt"], i["id"]))
    return c.respond(200, {"photos": items})


def valid_pid(pid):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", str(pid)))


def exists(c, key):
    try:
        c.s3.head_object(Bucket=c.bucket, Key=key)
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def face_ids_for(c, event_id, pid):
    ids, kwargs = [], {"CollectionId": f"ev-{event_id}", "MaxResults": 4096}
    while True:
        try:
            page = c.rek.list_faces(**kwargs)
        except c.rek.exceptions.ResourceNotFoundException:
            return ids
        ids += [f["FaceId"] for f in page.get("Faces", []) if f.get("ExternalImageId") == pid]
        if not page.get("NextToken"):
            return ids
        kwargs["NextToken"] = page["NextToken"]


def drop_faces(c, event_id, face_ids):
    for i in range(0, len(face_ids), 4096):
        try:
            c.rek.delete_faces(CollectionId=f"ev-{event_id}", FaceIds=face_ids[i:i + 4096])
        except c.rek.exceptions.ResourceNotFoundException:
            return


def index_faces(c, event_id, image_bytes, pid):
    kwargs = dict(CollectionId=f"ev-{event_id}", Image={"Bytes": image_bytes}, ExternalImageId=pid,
                  MaxFaces=100, QualityFilter="AUTO", DetectionAttributes=[])
    try:
        resp = c.rek.index_faces(**kwargs)
    except c.rek.exceptions.ResourceNotFoundException:
        try:
            c.rek.create_collection(CollectionId=f"ev-{event_id}")
        except c.rek.exceptions.ResourceAlreadyExistsException:
            pass
        resp = c.rek.index_faces(**kwargs)
    return [r["Face"]["FaceId"] for r in resp.get("FaceRecords", [])]


def forget_lists(c, event_id):
    for vis in VISIBILITIES:
        c.list_cache.pop((event_id, vis), None)


def match_live(c, ev, pid, face_ids):
    """Add a newly private photo to the galleries of guests who turned on live updates."""
    if not ev.get("live"):
        return
    eid, found = ev["eventId"], {}
    for fid in face_ids:
        try:
            resp = c.rek.search_faces(CollectionId=f"ev-{eid}", FaceId=fid, FaceMatchThreshold=LIVE_MATCH_THRESHOLD, MaxFaces=100)
        except c.rek.exceptions.ResourceNotFoundException:
            return
        except ClientError as err:
            print(f"live match failed: {err}")
            continue
        for m in resp.get("FaceMatches", []):
            ext = m["Face"].get("ExternalImageId", "")
            if ext.startswith("live_"):
                found[ext[5:]] = m["Face"]["FaceId"]
    for live_id, face_id in found.items():
        try:
            c.table.update_item(Key={"pk": f"EVENT#{eid}", "sk": f"LIVE#{live_id}"}, UpdateExpression="ADD photoIds :p",
                                ConditionExpression="attribute_exists(pk) AND expiresAt > :now",
                                ExpressionAttributeValues={":p": {pid}, ":now": int(time.time())})
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            drop_faces(c, eid, [face_id])  # guest stopped or their session expired


def current_place(c, eid, pid):
    return next((v for v in VISIBILITIES if exists(c, f"events/{eid}/{v}/thumb/{pid}.jpg")), None)


def relocate(c, ev, pid, target, updates):
    """Move a photo between public, private and held, keeping faces and live galleries in step."""
    eid, base = ev["eventId"], f"events/{ev['eventId']}"
    source = current_place(c, eid, pid)
    if not source:
        raise c.Err(404, "Photo not found.")
    key = {"pk": f"EVENT#{eid}", "sk": f"PHOTO#{pid}"}
    rec = c.table.get_item(Key=key).get("Item") or {**key, "photoId": pid, "createdAt": int(time.time())}
    if source != target:
        for size in SIZES:  # copy first so the photo is never missing
            if size == "original" and not exists(c, f"{base}/{source}/original/{pid}.jpg"):
                continue  # hi-res window has ended; the screen-size copy moves on its own
            c.s3.copy_object(Bucket=c.bucket, CopySource={"Bucket": c.bucket, "Key": f"{base}/{source}/{size}/{pid}.jpg"},
                             Key=f"{base}/{target}/{size}/{pid}.jpg", ContentType="image/jpeg", MetadataDirective="REPLACE")
    face_side = rec.get("intended", "private") if target == "held" else target
    if face_side == "private":
        if not rec.get("faceIds"):
            web = c.s3.get_object(Bucket=c.bucket, Key=f"{base}/{target}/web/{pid}.jpg")["Body"].read()
            rec["faceIds"] = index_faces(c, eid, web, pid)
            match_live(c, ev, pid, rec["faceIds"])
    else:
        drop_faces(c, eid, list(rec.get("faceIds") or []) or face_ids_for(c, eid, pid))
        rec["faceIds"] = []
    if source != target:
        for size in SIZES:
            c.s3.delete_object(Bucket=c.bucket, Key=f"{base}/{source}/{size}/{pid}.jpg")
    rec.update(visibility=target, **updates)
    if target != "held":
        rec.pop("intended", None)
    c.table.put_item(Item=rec)
    forget_lists(c, eid)
    return target


def move(c, ev, data, who="you"):
    pid, target = str(data.get("id", "")), data.get("visibility")
    if target not in ("public", "private") or not valid_pid(pid):
        raise c.Err(400, "Send a photo id and public or private.")
    if current_place(c, ev["eventId"], pid) == target:
        return c.respond(200, {"id": pid, "visibility": target})
    relocate(c, ev, pid, target, {"source": "you", "reason": f"Set to {target} by {who}", "confidence": "1"})
    return c.respond(200, {"id": pid, "visibility": target})


def move_many(c, ev, data, who="you"):
    target = data.get("visibility")
    if target not in ("public", "private"):
        raise c.Err(400, "Choose public or private.")
    done, failed = [], []
    for pid in id_list(c, data):
        try:
            if current_place(c, ev["eventId"], pid) != target:
                relocate(c, ev, pid, target, {"source": "you", "reason": f"Set to {target} by {who}", "confidence": "1"})
            done.append(pid)
        except Exception as err:  # keep going; report what didn't move
            print(f"bulk move {pid} failed: {err}")
            failed.append(pid)
    return c.respond(200, {"moved": done, "failed": failed, "visibility": target})


def approve(c, ev, data, who="you"):
    eid, done = ev["eventId"], []
    for pid in id_list(c, data):
        if current_place(c, eid, pid) != "held":
            continue
        rec = c.table.get_item(Key={"pk": f"EVENT#{eid}", "sk": f"PHOTO#{pid}"}).get("Item") or {}
        target = rec.get("intended") if rec.get("intended") in ("public", "private") else "private"
        relocate(c, ev, pid, target, {"approvedBy": who, "approvedAt": int(time.time())})
        done.append({"id": pid, "visibility": target})
    return c.respond(200, {"approved": done})


def id_list(c, data, limit=100):
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not 1 <= len(ids) <= limit:
        raise c.Err(400, f"Send between 1 and {limit} photo ids.")
    return [str(i) for i in ids if valid_pid(i)]


def delete_photos(c, ev, data):
    eid, deleted = ev["eventId"], []
    for pid in id_list(c, data):
        key = {"pk": f"EVENT#{eid}", "sk": f"PHOTO#{pid}"}
        rec = c.table.get_item(Key=key).get("Item") or {}
        faces = list(rec.get("faceIds") or [])
        if not faces and current_place(c, eid, pid) in ("private", "held"):
            faces = face_ids_for(c, eid, pid)
        drop_faces(c, eid, faces)
        objs = [{"Key": f"events/{eid}/{v}/{s}/{pid}.jpg"} for v in VISIBILITIES for s in SIZES]
        c.s3.delete_objects(Bucket=c.bucket, Delete={"Objects": objs, "Quiet": True})
        c.table.delete_item(Key=key)
        deleted.append(pid)
    forget_lists(c, eid)
    return c.respond(200, {"deleted": deleted})


def invoke_processor(payload):
    lambda_client.invoke(FunctionName=PROCESSOR_FUNCTION, InvocationType="Event", Payload=json.dumps(payload).encode())


def resort(c, org_id, ev, data):
    if not ev.get("aiSorting"):
        raise c.Err(403, "AI sorting is off for this event. Turn it on in Settings, or sort the photos yourself.")
    eid, started = ev["eventId"], 0
    for pid in id_list(c, data):
        c.table.update_item(Key={"pk": f"EVENT#{eid}", "sk": f"PHOTO#{pid}"},
                            UpdateExpression="SET #s = :s, reason = :r, confidence = :c, photoId = :p",
                            ExpressionAttributeNames={"#s": "source"},
                            ExpressionAttributeValues={":s": "ai", ":r": "Re-sorting with AI...", ":c": "0", ":p": pid})
        invoke_processor({"resort": pid, "orgId": org_id, "eventId": eid, "bucket": c.bucket})
        started += 1
    return c.respond(202, {"started": started})


def delete_all(c, org_id, ev, data):
    if str(data.get("confirm", "")).strip().upper() != DELETE_PHRASE:
        raise c.Err(400, f'Type "{DELETE_PHRASE}" to confirm.')
    invoke_processor({"deleteAll": True, "orgId": org_id, "eventId": ev["eventId"], "bucket": c.bucket,
                      "includeHosts": bool(data.get("includeHosts"))})
    forget_lists(c, ev["eventId"])
    return c.respond(202, {"started": True})


# ---------- Rules ----------

def get_rules(c, ev):
    rules = ev.get("sortingRules") or ""
    return c.respond(200, {"rules": rules or c.default_rules, "isDefault": not rules})


def save_rules(c, ev, data):
    text = str(data.get("rules", "")).strip()
    if len(text) > 8000:
        raise c.Err(400, "Keep the rules under 8,000 characters.")
    key = {"pk": ev["pk"], "sk": ev["sk"]}
    if text:
        c.table.update_item(Key=key, UpdateExpression="SET sortingRules = :r", ExpressionAttributeValues={":r": text})
    else:
        c.table.update_item(Key=key, UpdateExpression="REMOVE sortingRules")
    return c.respond(200, {"rules": text or c.default_rules, "isDefault": not text})


# ---------- Hosts (couple, birthday person...) ----------

def host_faces(c, event_id):
    faces, kwargs = [], {"CollectionId": f"ev-{event_id}-hosts", "MaxResults": 4096}
    while True:
        try:
            page = c.rek.list_faces(**kwargs)
        except c.rek.exceptions.ResourceNotFoundException:
            return []
        faces += page.get("Faces", [])
        if not page.get("NextToken"):
            return faces
        kwargs["NextToken"] = page["NextToken"]


def host_counts(c, ev):
    counts = {}
    for f in host_faces(c, ev["eventId"]):
        name = f.get("ExternalImageId", "")
        counts[name] = counts.get(name, 0) + 1
    return c.respond(200, {"hosts": [{"name": n, "photos": k} for n, k in sorted(counts.items())]})


def host_name(c, data):
    name = re.sub(r"[^a-z0-9-]+", "-", str(data.get("name", "")).strip().lower()).strip("-")[:40]
    if not name:
        raise c.Err(400, "Give the person a name, for example bride, groom or tola.")
    return name


def decode_image(c, value, limit=4_000_000):
    value = str(value or "")
    if "," in value[:100]:
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(value, validate=True)
    except Exception:
        raise c.Err(400, "That photo didn't upload correctly.")
    if not data or len(data) > limit:
        raise c.Err(400, "That photo is too large.")
    return data


def add_host(c, ev, data):
    name, image = host_name(c, data), decode_image(c, data.get("image"))
    collection = f"ev-{ev['eventId']}-hosts"
    try:
        c.rek.create_collection(CollectionId=collection)
    except c.rek.exceptions.ResourceAlreadyExistsException:
        pass
    try:
        resp = c.rek.index_faces(CollectionId=collection, Image={"Bytes": image}, ExternalImageId=name,
                                 MaxFaces=1, QualityFilter="MEDIUM", DetectionAttributes=[])
    except c.rek.exceptions.InvalidParameterException:
        resp = {}
    if not resp.get("FaceRecords"):
        raise c.Err(422, "No clear face found. Use a photo where their face is large, sharp and facing the camera.")
    return host_counts(c, ev)


def clear_host(c, ev, data):
    name = host_name(c, data)
    ids = [f["FaceId"] for f in host_faces(c, ev["eventId"]) if f.get("ExternalImageId") == name]
    if ids:
        c.rek.delete_faces(CollectionId=f"ev-{ev['eventId']}-hosts", FaceIds=ids)
    return host_counts(c, ev)


# ---------- Stats ----------

def stats(c, ev):
    eid = ev["eventId"]
    return c.respond(200, {
        "selfies": int(ev.get("statSelfies", 0)), "guestsFound": int(ev.get("statGuestsFound", 0)),
        "downloads": int(ev.get("statDownloads", 0)), "photoViews": int(ev.get("statViews", 0)),
        "publicPhotos": len(list_ids(c, eid, "public")), "privatePhotos": len(list_ids(c, eid, "private")),
        "heldPhotos": len(list_ids(c, eid, "held")), "liveGuests": int(ev.get("statLiveGuests", 0)),
        "hiresDownloads": int(ev.get("statHiresDownloads", 0)),
    })


# ---------- Event page details and settings ----------

def details_view(ev):
    d = dict(ev.get("details") or {})
    return {k: str(d.get(k, "")) for k in DETAIL_FIELDS}


def save_details(c, ev, data):
    d = details_view(ev)
    for field, limit in DETAIL_FIELDS.items():
        if field in data:
            text = str(data.get(field) or "").replace("\r\n", "\n").strip()
            if field != "schedule" and field != "welcome":
                text = re.sub(r"\s+", " ", text)
            d[field] = text[:limit]
    c.table.update_item(Key={"pk": ev["pk"], "sk": ev["sk"]}, UpdateExpression="SET details = :d", ExpressionAttributeValues={":d": d})
    return c.respond(200, d)


def settings_view(ev):
    return {"live": bool(ev.get("live")), "requireApproval": bool(ev.get("requireApproval")),
            "aiSorting": bool(ev.get("aiSorting")), "aiTrial": bool(ev.get("aiTrial")),
            "storageMode": ev.get("storageMode", "full"), "hiresDays": int(ev.get("hiresDays", 14))}


def save_settings(c, ev, data, role="team"):
    for field in ("live", "requireApproval"):
        if field in data:
            ev[field] = bool(data[field])
    names, values = {"#l": "live", "#a": "requireApproval"}, {":l": bool(ev.get("live")), ":a": bool(ev.get("requireApproval"))}
    sets = ["#l = :l", "#a = :a"]
    paid = [f for f in ("aiSorting", "storageMode", "hiresDays") if f in data]
    if paid and role != "team":
        raise c.Err(403, "Ask your photographer to change this.")
    if "aiSorting" in data:
        # Billing will gate this once plans are live; for now any studio member can switch it.
        ev["aiSorting"] = bool(data["aiSorting"])
        names["#ai"], values[":ai"] = "aiSorting", ev["aiSorting"]
        sets.append("#ai = :ai")
    if "storageMode" in data:
        if data["storageMode"] not in STORAGE_MODES:
            raise c.Err(400, "Choose full or window.")
        ev["storageMode"] = data["storageMode"]
        names["#sm"], values[":sm"] = "storageMode", ev["storageMode"]
        sets.append("#sm = :sm")
    if "hiresDays" in data:
        try:
            days = int(data["hiresDays"])
        except (TypeError, ValueError):
            days = 0
        if days not in HIRES_DAYS:
            raise c.Err(400, "Keep hi-res photos for 7, 14 or 30 days.")
        ev["hiresDays"] = days
        names["#hd"], values[":hd"] = "hiresDays", days
        sets.append("#hd = :hd")
    c.table.update_item(Key={"pk": ev["pk"], "sk": ev["sk"]}, UpdateExpression="SET " + ", ".join(sets),
                        ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    return c.respond(200, settings_view(ev))


# ---------- Event owners (the host) ----------

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


def owner_email(c, data):
    email = str(data.get("email", "")).strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise c.Err(400, "Enter a valid email address.")
    return email


def list_owners(c, ev):
    items = c.table.query(KeyConditionExpression=Key("pk").eq(f"EVENT#{ev['eventId']}") & Key("sk").begins_with("OWNER#"))["Items"]
    return c.respond(200, {"owners": [{"email": i["sk"].split("#", 1)[1], "addedAt": int(i.get("createdAt", 0))} for i in items]})


def add_owner(c, org_id, ev, data):
    email, now, eid = owner_email(c, data), int(time.time()), ev["eventId"]
    existing = c.table.query(KeyConditionExpression=Key("pk").eq(f"EVENT#{eid}") & Key("sk").begins_with("OWNER#"), Select="COUNT")
    if existing.get("Count", 0) >= 6:
        raise c.Err(400, "An event can have up to 6 owners.")
    c.table.put_item(Item={"pk": f"EVENT#{eid}", "sk": f"OWNER#{email}", "orgId": org_id, "createdAt": now})
    c.table.put_item(Item={"pk": f"OWNER#{email}", "sk": f"EVENT#{eid}", "orgId": org_id, "createdAt": now})
    return list_owners(c, ev)


def remove_owner(c, ev, data):
    email, eid = owner_email(c, data), ev["eventId"]
    c.table.delete_item(Key={"pk": f"EVENT#{eid}", "sk": f"OWNER#{email}"})
    c.table.delete_item(Key={"pk": f"OWNER#{email}", "sk": f"EVENT#{eid}"})
    return list_owners(c, ev)


# ---------- Studio branding ----------

def load_branding(c, org_id):
    org = c.table.get_item(Key={"pk": f"ORG#{org_id}", "sk": "META"}).get("Item") or {}
    b = dict(org.get("branding") or {})
    return org, {k: str(b.get(k, "")) for k in BRANDING_FIELDS} | {"hasLogo": bool(b.get("hasLogo")), "logoVersion": int(b.get("logoVersion", 0))}


def branding_view(c, org_id, org, b):
    out = {k: b[k] for k in BRANDING_FIELDS}
    out["name"] = out["name"] or org.get("name", "")
    out["logoUrl"] = presign(c, f"orgs/{org_id}/logo") if b["hasLogo"] else ""
    return out


def get_branding(c, org_id):
    org, b = load_branding(c, org_id)
    return c.respond(200, branding_view(c, org_id, org, b))


def save_branding(c, org_id, data):
    org, b = load_branding(c, org_id)
    for field, limit in BRANDING_FIELDS.items():
        if field in data:
            b[field] = re.sub(r"\s+", " ", str(data.get(field) or "")).strip()[:limit]
    if b["link"] and not re.match(r"^https?://", b["link"], re.I):
        b["link"] = "https://" + b["link"].lstrip("/")
    if b["link"] and not re.fullmatch(r"https?://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(:\d+)?(/[^\s<>\"']*)?", b["link"]):
        raise c.Err(400, "That website link doesn't look right.")
    b["instagram"] = b["instagram"].lstrip("@").split("/")[-1]
    c.table.update_item(Key={"pk": f"ORG#{org_id}", "sk": "META"}, UpdateExpression="SET branding = :b", ExpressionAttributeValues={":b": b})
    return c.respond(200, branding_view(c, org_id, org, b))


def save_logo(c, org_id, data):
    org, b = load_branding(c, org_id)
    image = str(data.get("image", ""))
    if not image:
        c.s3.delete_object(Bucket=c.bucket, Key=f"orgs/{org_id}/logo")
        b["hasLogo"] = False
    else:
        m = re.match(r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", image, re.S)
        if not m:
            raise c.Err(400, "Use a PNG, JPG or WebP logo.")
        raw = decode_image(c, m.group(2), 2_000_000)
        c.s3.put_object(Bucket=c.bucket, Key=f"orgs/{org_id}/logo", Body=raw, ContentType=m.group(1), CacheControl="no-cache")
        b["hasLogo"], b["logoVersion"] = True, int(time.time())
    c.table.update_item(Key={"pk": f"ORG#{org_id}", "sk": "META"}, UpdateExpression="SET branding = :b", ExpressionAttributeValues={":b": b})
    return c.respond(200, branding_view(c, org_id, org, b))


def public_branding(c, org_id):
    org, b = load_branding(c, org_id)
    return branding_view(c, org_id, org, b)
