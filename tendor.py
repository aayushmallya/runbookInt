import requests
import os
import time
import json
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("API_TOKEN")

WMT_URL = "https://wmt-freight-portal.vercel.app/api/sap/loads"
SHV_URL = "https://shv-logistics-tms.vercel.app/api/sor/loads"

SHV_BATCH_SIZE = 50  # SHV accepts at most 50 loads per POST

headers = {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def rateLimit(request_fn, max_retries=5):
    """
    Calls request_fn() (a zero-arg callable that performs one HTTP request)
    and retries on 429 using the server's Retry-After header. Returns the
    final requests.Response, whatever its status code.
    """
    attempt = 0
    while True:
        response = request_fn()

        if response.status_code != 429:
            return response

        attempt += 1
        if attempt > max_retries:
            return response

        retry_after = int(response.headers.get("Retry-After", 5))
        print(f"  429 rate limited — waiting {retry_after}s (attempt {attempt}/{max_retries})")
        time.sleep(retry_after)


# ---------------------------------------------------------------------------
# GET — Walmart open tenders
# ---------------------------------------------------------------------------

def get_tendor():
    """
    Fetches open tenders from the Walmart Freight Tender API.
    Returns the list of raw load dicts, or [] on failure.
    """
    response = rateLimit(lambda: requests.get(WMT_URL, headers=headers))

    if response.status_code == 401:
        print("Walmart GET failed: 401 — check API_TOKEN in .env")
        return []

    if response.status_code != 200:
        print(f"Walmart GET failed: {response.status_code} — {response.text}")
        return []

    body = response.json()
    loads = body.get("loads", [])
    print(f"Fetched {body.get('count', len(loads))} open tender(s) from {body.get('source', 'Walmart')}")
    return loads


# ---------------------------------------------------------------------------
# Transform — Walmart record -> SHV record
# ---------------------------------------------------------------------------

def swap_date(mmddyyyy):
    """MMDDYYYY -> DDMMYYYY. Returns None if the input isn't 8 digits."""
    if not mmddyyyy or not str(mmddyyyy).isdigit() or len(mmddyyyy) != 8:
        return None
    mm, dd, yyyy = mmddyyyy[:2], mmddyyyy[2:4], mmddyyyy[4:]
    return dd + mm + yyyy


def parse_weight(wgt):
    """'41,860 lbs' -> 41860 (int). Returns None if unparseable."""
    if not wgt:
        return None
    digits = "".join(ch for ch in str(wgt) if ch.isdigit())
    return int(digits) if digits else None


def map_equipment(mode):
    """
    Returns (equipment_type, flag_reason). equipment_type is None and
    flag_reason is set when the mode can't be confidently mapped.
    """
    m = (mode or "").strip().upper()
    if m.lower() == "ambient":
        return "Dry Van 53'", None
    if m.lower() in ("refrig", "freezer"):
        return "Reefer 53'", None
    if m.lower() == "fresh":
        return None, 'mode "FRESH" has no confirmed equipment mapping — needs follow-up with Walmart to confirm temp requirement'
    return None, f'unrecognized mode "{mode}"'


def transform_load(load):
    """
    Converts one Walmart tender record into an SHV payload dict.
    Returns (payload, issues) where issues is a list of strings; a
    non-empty issues list means this load should NOT be pushed as-is.
    """
    equipment_type, eq_issue = map_equipment(load.get("mode"))
    ship_date = swap_date(load.get("shp_dt"))
    delivery_date = swap_date(load.get("del_dt"))
    weight = parse_weight(load.get("wgt"))

    issues = []
    if eq_issue:
        issues.append(eq_issue)
    if ship_date is None:
        issues.append(f'unparseable ship date "{load.get("shp_dt")}"')
    if delivery_date is None:
        issues.append(f'unparseable delivery date "{load.get("del_dt")}"')
    if weight is None:
        issues.append(f'unparseable weight "{load.get("wgt")}"')

    payload = {
        "load_number": load.get("load_no"),
        "bol_number": load.get("frt_ord_no"),
        "shipper_name": load.get("shipper_nm"),
        "origin_city": load.get("orig_city"),
        "origin_state": load.get("orig_st"),
        "destination_city": load.get("dest_city"),
        "destination_state": load.get("dest_st"),
        "ship_date": ship_date,
        "delivery_date": delivery_date,
        "weight": weight,
        "equipment_type": equipment_type,
    }

    # Safety net: strip leading/trailing whitespace on every string field,
    # regardless of which ones were built above. Non-string values (weight,
    # None) pass through untouched.
    payload = {k: (v.strip() if isinstance(v, str) else v) for k, v in payload.items()}

    return payload, issues


def build_preview(loads):
    """
    Transforms every raw load and returns one entry per load:
      { "source": <raw walmart record>,
        "payload": <SHV payload dict, or None if flagged>,
        "flagged": bool,
        "issues": [str, ...] }
    Used by both the CLI flow (via sanitize) and the web API layer, so a
    caller can render the full raw + transformed picture in one shot.
    """
    preview = []
    for load in loads:
        payload, issues = transform_load(load)
        preview.append({
            "source": load,
            "payload": None if issues else payload,
            "flagged": bool(issues),
            "issues": issues,
        })
    return preview


def sanitize(loads):
    """
    Transforms every raw load. Returns (clean, flagged):
      clean   -> list of SHV-ready payload dicts, safe to push
      flagged -> list of (load_no, issues) tuples held back for manual review
    """
    preview = build_preview(loads)
    clean = [p["payload"] for p in preview if not p["flagged"]]
    flagged = [(p["source"].get("load_no"), p["issues"]) for p in preview if p["flagged"]]
    return clean, flagged


# ---------------------------------------------------------------------------
# POST — SHV system of record
# ---------------------------------------------------------------------------

def post_tendor(loads):
    """
    Pushes a list of already-sanitized SHV payload dicts, chunked into
    batches of at most SHV_BATCH_SIZE. Returns a list of per-batch
    (status_code, response_body) tuples.
    """
    if not loads:
        print("No clean loads to push.")
        return []

    results = []
    for i in range(0, len(loads), SHV_BATCH_SIZE):
        batch = loads[i:i + SHV_BATCH_SIZE]
        body = {"loads": batch}

        response = rateLimit(lambda: requests.post(
            SHV_URL,
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(body),
        ))

        try:
            parsed = response.json()
        except ValueError:
            parsed = {"raw": response.text}

        results.append((response.status_code, parsed))

        if response.status_code == 200:
            print(f"Batch {i // SHV_BATCH_SIZE + 1}: {parsed.get('message', 'accepted')}")
        elif response.status_code == 422:
            print(f"Batch {i // SHV_BATCH_SIZE + 1}: 422 — {len(parsed.get('rejected', []))} load(s) rejected")
            for r in parsed.get("rejected", []):
                print(f"  {r.get('load_number')}: {r.get('errors')}")
        elif response.status_code == 401:
            print("SHV POST failed: 401 — check API_TOKEN in .env")
        else:
            print(f"Batch {i // SHV_BATCH_SIZE + 1}: {response.status_code} — {parsed}")

    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    if not token:
        print("API_TOKEN not set — add it to .env")
        return

    raw_loads = get_tendor()
    if not raw_loads:
        return

    clean, flagged = sanitize(raw_loads)

    if flagged:
        print(f"\n{len(flagged)} load(s) held back from push:")
        for load_no, issues in flagged:
            print(f"  {load_no}: {'; '.join(issues)}")

    print(f"\nPushing {len(clean)} clean load(s) to SHV...")
    post_tendor(clean)


if __name__ == "__main__":
    main()