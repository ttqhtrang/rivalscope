import os
import json
import sqlite3
from collections import defaultdict
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# SQLite setup
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "rivalscope.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                domain      TEXT    NOT NULL,
                market      TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                result_json TEXT    NOT NULL
            )
        """)

init_db()

# ---------------------------------------------------------------------------
# DataForSEO config
# ---------------------------------------------------------------------------
DFS_EMAIL    = os.environ.get("DATAFORSEO_EMAIL",    "")
DFS_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "")
SANDBOX      = os.environ.get("DATAFORSEO_SANDBOX", "false").lower() == "true"
DFS_BASE     = "https://sandbox.dataforseo.com/v3" if SANDBOX else "https://api.dataforseo.com/v3"

# location_code, language_code per market
MARKET_MAP = {
    "vn": (2704, "vi"),
    "us": (2840, "en"),
    "sg": (2702, "en"),
    "au": (2036, "en"),
    "gb": (2826, "en"),
}

# ---------------------------------------------------------------------------
# Topic rules (Vietnamese-first, fallback English)
# ---------------------------------------------------------------------------
TOPIC_RULES = [
    # Xe cộ / phương tiện
    ("Xe máy điện",        ["xe máy điện", "xe điện", "xe đạp điện", "scooter điện",
                             "vin fast", "vinfast", "honda điện", "yamaha điện", "peugeot",
                             "xe may dien", "xe dap dien"]),
    ("Xe máy / Mô tô",     ["xe máy", "mô tô", "motor", "honda", "yamaha", "suzuki",
                             "kawasaki", "wave", "exciter", "winner", "sh", "air blade",
                             "xe so", "xe tay ga", "xe côn tay"]),
    ("Ô tô / Xe hơi",      ["ô tô", "xe hơi", "xe oto", "sedan", "suv", "crossover",
                             "toyota", "hyundai", "kia", "mazda", "ford", "mercedes", "bmw",
                             "vios", "camry", "civic", "accent"]),
    ("Xe đạp",             ["xe đạp", "xe dap", "đạp điện", "mountain bike", "road bike"]),

    # Điện thoại / Công nghệ
    ("Điện thoại",         ["iphone", "samsung", "oppo", "xiaomi", "vivo", "realme", "pixel",
                             "điện thoại", "smartphone", "phone", "nokia"]),
    ("Laptop / Máy tính",  ["laptop", "macbook", "máy tính", "notebook", "surface",
                             "dell", "asus", "lenovo", "acer", "gaming pc"]),
    ("Phụ kiện",           ["ốp lưng", "sạc", "cáp", "tai nghe", "airpods", "phụ kiện",
                             "case", "charger", "chuột", "bàn phím", "earphone"]),
    ("Tivi / Màn hình",    ["tivi", " tv ", "smart tv", "màn hình", "monitor", "oled", "qled"]),
    ("Điện máy gia dụng",  ["tủ lạnh", "máy giặt", "điều hoà", "máy lạnh", "lò vi sóng",
                             "nồi cơm", "bếp điện", "quạt điện", "máy hút bụi"]),

    # Nội dung / Intent
    ("Review / So sánh",   ["review", "so sánh", "đánh giá", "có nên mua", "tốt nhất",
                             "nên chọn", "nên mua", "so sanh", "danh gia", "comparison",
                             "bảng giá", "thông số"]),
    ("Giá / Mua sắm",      ["giá", "mua", "bán", "khuyến mãi", "giảm giá", "trả góp",
                             "giá tốt", "deal", "ưu đãi", "flash sale", "price", "buy", "cheap"]),
    ("Sửa chữa / Hỗ trợ", ["sửa", "thay", "lỗi", "không lên nguồn", "hư", "bị vỡ",
                             "cách fix", "khắc phục", "repair", "fix", "bảo dưỡng",
                             "thay nhớt", "thay lốp", "thay phanh"]),
]

def categorize(keyword: str) -> str:
    kw = keyword.lower()
    for topic, terms in TOPIC_RULES:
        if any(t in kw for t in terms):
            return topic
    return "Khác"

# ---------------------------------------------------------------------------
# DataForSEO helpers
# ---------------------------------------------------------------------------

def dfs_auth():
    if not DFS_EMAIL or not DFS_PASSWORD:
        raise ValueError(
            "Chưa cấu hình DataForSEO credentials.\n"
            "Chạy lệnh:\n"
            "  export DATAFORSEO_EMAIL=your@email.com\n"
            "  export DATAFORSEO_PASSWORD=your_password"
        )
    return (DFS_EMAIL, DFS_PASSWORD)


def dfs_post(endpoint: str, payload: list, timeout: int = 30) -> dict:
    r = requests.post(
        f"{DFS_BASE}{endpoint}",
        auth=dfs_auth(),
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def check_task(response: dict, label: str) -> list:
    tasks = response.get("tasks", [])
    if not tasks:
        raise RuntimeError(f"DataForSEO không trả về tasks cho {label}")
    t = tasks[0]
    if t.get("status_code") not in (20000, 20100):
        raise RuntimeError(f"DataForSEO lỗi [{label}]: {t.get('status_message', 'unknown')}")
    result = t.get("result") or []
    return result[0].get("items") or [] if result else []

# ---------------------------------------------------------------------------
# Domain analysis
# ---------------------------------------------------------------------------

def analyze_domain(domain: str, location_code: int, language_code: str) -> dict:
    # --- Ranked keywords (up to 500, sorted by ETV desc) ---
    kw_resp = dfs_post(
        "/dataforseo_labs/google/ranked_keywords/live",
        [{
            "target":        domain,
            "location_code": location_code,
            "language_code": language_code,
            "limit":         500,
            "order_by":      ["ranked_serp_element.serp_item.etv,desc"],
            "filters":       [["ranked_serp_element.serp_item.type", "=", "organic"]],
        }],
        timeout=40,
    )
    kw_tasks = kw_resp.get("tasks", [{}])
    kw_task  = kw_tasks[0] if kw_tasks else {}
    if kw_task.get("status_code") not in (20000, 20100):
        raise RuntimeError(f"DataForSEO error: {kw_task.get('status_message','unknown')}")

    kw_result     = (kw_task.get("result") or [{}])[0]
    total_kws     = kw_result.get("total_count", 0)
    items         = kw_result.get("items") or []

    # --- Process items ---
    topic_etv: dict[str, float]  = defaultdict(float)
    pages:     dict[str, dict]   = defaultdict(lambda: {"etv": 0.0, "kw_count": 0, "url": ""})
    top_kws:   list[dict]        = []
    total_etv  = 0.0

    for item in items:
        kw_data  = item.get("keyword_data") or {}
        kw       = kw_data.get("keyword", "")
        kw_info  = kw_data.get("keyword_info") or {}
        volume   = int(kw_info.get("search_volume") or 0)

        serp_item = (item.get("ranked_serp_element") or {}).get("serp_item") or {}
        position  = int(serp_item.get("rank_group")   or 0)
        url       = serp_item.get("url", "")
        etv       = float(serp_item.get("etv")         or 0)  # estimated monthly visits

        topic      = categorize(kw)
        topic_etv[topic] += etv
        total_etv  += etv

        if url:
            pages[url]["etv"]      += etv
            pages[url]["kw_count"] += 1
            pages[url]["url"]       = url

        if len(top_kws) < 30:
            top_kws.append({
                "keyword":     kw,
                "position":    position,
                "volume":      volume,
                "etv":         round(etv),
                "url":         url,
                "topic":       topic,
            })

    sorted_topics = sorted(topic_etv.items(), key=lambda x: x[1], reverse=True)
    top_pages     = sorted(pages.values(), key=lambda x: x["etv"], reverse=True)[:10]

    return {
        "overview": {
            "domain":         domain,
            "total_traffic":  round(total_etv),
            "total_keywords": total_kws,
        },
        "topics": [
            {"topic": t, "etv": round(e), "pct": round(e / total_etv * 100, 1) if total_etv else 0}
            for t, e in sorted_topics
        ],
        "top_keywords": top_kws,
        "top_pages":    [
            {"url": p["url"], "etv": round(p["etv"]), "kw_count": p["kw_count"]}
            for p in top_pages
        ],
    }

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body     = request.get_json() or {}
    raw      = body.get("domains", [])
    database = body.get("database", "vn")

    domains = [
        d.strip().lower()
         .replace("https://", "").replace("http://", "")
         .split("/")[0]
        for d in raw if d.strip()
    ]

    if not domains:
        return jsonify({"error": "Vui lòng nhập ít nhất 1 domain"}), 400

    location_code, language_code = MARKET_MAP.get(database, (2704, "vi"))

    results = {}
    for domain in domains:
        try:
            data = analyze_domain(domain, location_code, language_code)
            results[domain] = data
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO analyses (domain, market, result_json) VALUES (?, ?, ?)",
                    (domain, database, json.dumps(data, ensure_ascii=False)),
                )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except requests.exceptions.Timeout:
            results[domain] = {"error": f"Timeout khi phân tích {domain}. Thử lại sau."}
        except Exception as exc:
            results[domain] = {"error": str(exc)}

    return jsonify(results)


@app.route("/api/history")
def api_history():
    limit = min(int(request.args.get("limit", 20)), 100)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, domain, market, created_at FROM analyses ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/<int:record_id>")
def api_history_detail(record_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, domain, market, created_at, result_json FROM analyses WHERE id = ?",
            (record_id,),
        ).fetchone()
    if not row:
        return jsonify({"error": "Không tìm thấy record"}), 404
    data = dict(row)
    data["result"] = json.loads(data.pop("result_json"))
    return jsonify(data)


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "credentials": bool(DFS_EMAIL and DFS_PASSWORD),
        "sandbox": SANDBOX,
        "api_base": DFS_BASE,
    })


@app.route("/api/test-credentials")
def test_credentials():
    """Test DataForSEO credentials — mở /api/test-credentials trên browser để kiểm tra"""
    if not DFS_EMAIL or not DFS_PASSWORD:
        return jsonify({"ok": False, "error": "Chưa có credentials trong environment variables"}), 400

    # Gọi endpoint nhẹ nhất của DataForSEO để kiểm tra auth
    try:
        r = requests.get(
            "https://api.dataforseo.com/v3/dataforseo_labs/status",
            auth=(DFS_EMAIL, DFS_PASSWORD),
            timeout=10,
        )
        if r.status_code == 200:
            return jsonify({"ok": True, "message": "Credentials hợp lệ!", "email": DFS_EMAIL})
        else:
            return jsonify({
                "ok": False,
                "status_code": r.status_code,
                "email_used": DFS_EMAIL,
                "response": r.text[:500],
                "hint": (
                    "401 = sai password. Vào dataforseo.com → Profile → "
                    "đổi password hoặc kiểm tra lại biến DATAFORSEO_PASSWORD trên Render"
                )
            }), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n🔭 RivalScope đang chạy → http://localhost:{port}\n")
    app.run(host="0.0.0.0", debug=True, port=port)
