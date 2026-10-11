"""Platform photo processor, triggered by uploads to
    incoming/<orgId>/<eventId>/<public|private|auto>/<uploader>/<rand>__<name>

For each file:
  1. make thumb (480px) and web (2560px) versions
  2. decide visibility: the photographer's folder wins; otherwise, if every enrolled host
     (e.g. the couple) is recognised it's public; otherwise AI applies the event's rules;
     any doubt or any AI failure -> private
  3. index faces of private photos into the event's collection ev-<eventId>
  4. write events/<eventId>/<visibility>/<thumb|web|original>/<photoId>.jpg and a photo record
  5. delete the incoming file
Ported from the wedding app's processor; everything is scoped by event.
"""
import hashlib
import io
import os
import re
import time
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 200_000_000

TABLE = os.environ["TABLE"]
MODEL_ID = os.environ["MODEL_ID"]
PUBLIC_CONFIDENCE = float(os.environ.get("PUBLIC_CONFIDENCE", "0.8"))
HOST_MATCH_THRESHOLD = 90
MAX_FACES_CHECKED = 15

retry = Config(retries={"max_attempts": 10, "mode": "adaptive"})
s3 = boto3.client("s3", config=retry)
rekognition = boto3.client("rekognition", config=retry)
bedrock = boto3.client("bedrock-runtime", config=retry)
table = boto3.resource("dynamodb").Table(TABLE)

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

KEY_RE = re.compile(r"^incoming/([a-z0-9]{12})/([a-z0-9]{12})/(public|private|auto)/([a-z0-9-]+)/(.+)$")


def handler(event, _context):
    for record in event.get("Records", []):
        process(record["s3"]["bucket"]["name"], unquote_plus(record["s3"]["object"]["key"]))


def process(bucket, key):
    m = KEY_RE.match(key)
    if not m:
        print(f"Ignoring unexpected key {key}")
        return
    org_id, event_id, folder, uploader, filename = m.groups()
    ev = table.get_item(Key={"pk": f"ORG#{org_id}", "sk": f"EVENT#{event_id}"}).get("Item")
    if not ev or ev.get("status") != "active":
        print(f"{key}: event missing or inactive, discarding")
        s3.delete_object(Bucket=bucket, Key=key)
        return

    original_name = filename.split("__", 1)[-1]
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    pid = photo_id(original_name, data)
    base = f"events/{event_id}"
    if any(exists(bucket, f"{base}/{v}/thumb/{pid}.jpg") for v in ("public", "private")):
        print(f"{key}: already published as {pid}, removing duplicate")
        s3.delete_object(Bucket=bucket, Key=key)
        return

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    thumb, web = resized_jpeg(img, 480, 78), resized_jpeg(img, 2560, 85)
    if original_name.lower().endswith((".jpg", ".jpeg")):
        original = data
    else:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95)
        original = buf.getvalue()

    if folder == "auto":
        decision = decide(event_id, ev, img)
    else:
        decision = {"visibility": folder, "confidence": 1.0, "reason": "Sorted by photographer", "source": "photographer"}
    vis = decision["visibility"]

    face_ids = index_faces(event_id, web, pid) if vis == "private" else []
    for size, body in (("original", original), ("web", web), ("thumb", thumb)):  # thumb last = published
        s3.put_object(Bucket=bucket, Key=f"{base}/{vis}/{size}/{pid}.jpg", Body=body, ContentType="image/jpeg")
    table.put_item(Item={
        "pk": f"EVENT#{event_id}", "sk": f"PHOTO#{pid}", "photoId": pid, "visibility": vis,
        "source": decision["source"], "reason": decision["reason"][:500],
        "confidence": str(round(decision["confidence"], 2)), "uploader": uploader,
        "fileName": original_name, "faceIds": face_ids, "createdAt": int(time.time()),
    })
    s3.delete_object(Bucket=bucket, Key=key)
    print(f"{key}: published {vis} {pid} ({decision['source']}: {decision['reason']}), {len(face_ids)} face(s)")


def decide(event_id, ev, img):
    hosts, found = hosts_in_photo(event_id, img)
    if hosts and found == hosts:
        return {"visibility": "public", "confidence": 1.0, "reason": "All hosts are in this photo", "source": "hosts"}
    return classify(ev, resized_jpeg(img, 1568, 80), hosts, found)


# ---------- Host recognition (couple, birthday person, etc.) ----------

_hosts_cache = {}


def enrolled_hosts(event_id):
    cached = _hosts_cache.get(event_id)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    names, kwargs = set(), {"CollectionId": f"ev-{event_id}-hosts", "MaxResults": 4096}
    while True:
        try:
            page = rekognition.list_faces(**kwargs)
        except rekognition.exceptions.ResourceNotFoundException:
            break
        names |= {f["ExternalImageId"] for f in page.get("Faces", []) if f.get("ExternalImageId")}
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    _hosts_cache[event_id] = (names, time.time())
    return names


def hosts_in_photo(event_id, img):
    hosts = enrolled_hosts(event_id)
    found = set()
    if not hosts:
        return hosts, found
    small = img.copy()
    small.thumbnail((2560, 2560), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    try:
        faces = rekognition.detect_faces(Image={"Bytes": buf.getvalue()}).get("FaceDetails", [])
    except Exception as err:
        print(f"Face detection failed: {err}")
        return hosts, found
    w, h = small.size
    faces.sort(key=lambda f: f["BoundingBox"]["Width"] * f["BoundingBox"]["Height"], reverse=True)
    for face in faces[:MAX_FACES_CHECKED]:
        b = face["BoundingBox"]
        bw, bh = b["Width"] * w, b["Height"] * h
        if min(bw, bh) < 40:
            continue
        left, top = max(0, b["Left"] * w - bw * 0.5), max(0, b["Top"] * h - bh * 0.5)
        crop = small.crop((int(left), int(top), int(min(w, left + bw * 2)), int(min(h, top + bh * 2))))
        cbuf = io.BytesIO()
        crop.save(cbuf, "JPEG", quality=90)
        try:
            resp = rekognition.search_faces_by_image(CollectionId=f"ev-{event_id}-hosts", Image={"Bytes": cbuf.getvalue()},
                                                     FaceMatchThreshold=HOST_MATCH_THRESHOLD, MaxFaces=5)
        except rekognition.exceptions.ResourceNotFoundException:
            return hosts, found
        except rekognition.exceptions.InvalidParameterException:
            continue
        found |= {mm["Face"].get("ExternalImageId") for mm in resp.get("FaceMatches", [])} & hosts
        if found == hosts:
            break
    return hosts, found


# ---------- AI sorting ----------

CLASSIFY_TOOL = {"toolSpec": {
    "name": "record_decision",
    "description": "Record whether this event photo is public or private.",
    "inputSchema": {"json": {"type": "object", "properties": {
        "visibility": {"type": "string", "enum": ["public", "private"]},
        "confidence": {"type": "number", "description": "0 to 1: how sure you are"},
        "reason": {"type": "string", "description": "One short sentence describing the photo and why"},
    }, "required": ["visibility", "confidence", "reason"]}},
}}


def classify(ev, image_bytes, hosts=frozenset(), found=frozenset()):
    if not hosts:
        note = "No reference faces of the hosts have been added, so you can't be told who they are."
    elif found:
        note = f"Face matching recognised these hosts: {', '.join(sorted(found))}. Not recognised: {', '.join(sorted(hosts - found)) or 'none'}."
    else:
        note = "Face matching did not recognise any of the hosts in this photo."
    system = (
        "You sort event photos for a private photo-sharing site. Public photos are shown to every guest. "
        "Private photos are shown only to the people who appear in them. Apply the event's rules below. "
        "When a photo could reasonably go either way, choose private: showing a guest's photo to everyone "
        "is worse than hiding it. Never identify anyone; describe people only generically.\n\n"
        f"The event's rules:\n{ev.get('sortingRules') or DEFAULT_RULES}\n\n"
        f"About this photo: {note} Trust this over your own guess about who the hosts are."
    )
    try:
        resp = bedrock.converse(
            modelId=MODEL_ID, system=[{"text": system}],
            messages=[{"role": "user", "content": [
                {"image": {"format": "jpeg", "source": {"bytes": image_bytes}}},
                {"text": "Sort this photo using the rules, then call record_decision."}]}],
            toolConfig={"tools": [CLASSIFY_TOOL], "toolChoice": {"tool": {"name": "record_decision"}}},
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        call = next(c["toolUse"]["input"] for c in resp["output"]["message"]["content"] if "toolUse" in c)
        vis = call.get("visibility")
        conf = max(0.0, min(1.0, float(call.get("confidence", 0))))
        reason = str(call.get("reason", "")).strip() or "No reason given"
    except Exception as err:
        print(f"AI sorting failed, defaulting to private: {err}")
        return {"visibility": "private", "confidence": 0.0, "reason": "AI sorting unavailable, kept private", "source": "ai"}
    if vis == "public" and conf < PUBLIC_CONFIDENCE:
        return {"visibility": "private", "confidence": conf, "reason": f"Leaning public but not sure enough: {reason}", "source": "ai"}
    return {"visibility": vis if vis in ("public", "private") else "private", "confidence": conf, "reason": reason, "source": "ai"}


# ---------- Helpers ----------

def index_faces(event_id, image_bytes, pid):
    collection = f"ev-{event_id}"
    kwargs = dict(CollectionId=collection, Image={"Bytes": image_bytes}, ExternalImageId=pid,
                  MaxFaces=100, QualityFilter="AUTO", DetectionAttributes=[])
    try:
        resp = rekognition.index_faces(**kwargs)
    except rekognition.exceptions.ResourceNotFoundException:
        try:
            rekognition.create_collection(CollectionId=collection)
        except rekognition.exceptions.ResourceAlreadyExistsException:
            pass
        resp = rekognition.index_faces(**kwargs)
    return [r["Face"]["FaceId"] for r in resp.get("FaceRecords", [])]


def photo_id(name, data):
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(name)[0])[:60]
    return f"{stem}_{hashlib.sha1(data).hexdigest()[:8]}"


def resized_jpeg(img, max_side, quality):
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    copy.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def exists(bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
