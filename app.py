from flask import Flask, jsonify, render_template

from tendor import get_tendor, build_preview, post_tendor, token

app = Flask(__name__)


def _require_token():
    if not token:
        return jsonify({"error": "API_TOKEN not set — add it to .env on the backend"}), 500
    return None


@app.route("/", methods=["GET"])
def index():
    """Serves the frontend itself — one app, one port, same origin as /api/*."""
    return render_template("index.html")


@app.route("/api/fetch", methods=["GET"])
def api_fetch():
    """
    Pulls open tenders from Walmart and returns the raw + transformed
    preview for every load, so the frontend can render the raw table and
    flag anything that isn't push-ready — without ever seeing the token.
    """
    err = _require_token()
    if err:
        return err

    raw = get_tendor()
    preview = build_preview(raw)
    return jsonify({"count": len(raw), "loads": preview})


@app.route("/api/push", methods=["POST"])
def api_push():
    """
    Re-fetches current tenders, sanitizes them, and pushes every clean load
    to SHV. Returns the same preview list plus the SOR's accept/reject
    results so the frontend can render both in one response.
    """
    err = _require_token()
    if err:
        return err

    raw = get_tendor()
    preview = build_preview(raw)
    clean_payloads = [p["payload"] for p in preview if not p["flagged"]]

    batch_results = post_tendor(clean_payloads)

    accepted, rejected = [], []
    for status, body in batch_results:
        if isinstance(body, dict):
            accepted += body.get("accepted", [])
            rejected += body.get("rejected", [])

    return jsonify({
        "loads": preview,
        "accepted": accepted,
        "rejected": rejected,
        "batches": [{"status": s, "body": b} for s, b in batch_results],
    })


if __name__ == "__main__":
    app.run(port=5000, debug=True)