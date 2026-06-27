import os
import json
from collections import defaultdict
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# DataForSEO config
# ---------------------------------------------------------------------------
DFS_EMAIL    = os.environ.get("DATAFORSEO_EMAIL",    "")
DFS_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "")
DFS_BASE     = "https://api.dataforseo.com/v3"

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
    ("Điện thoại",         ["iphone", "samsung", "oppo", "xiaomi", "vivo", "realme", "pixel",
                             "điện thoại", "smartphone", "phone", "nokia"]),
    ("Laptop / Máy tính",  ["laptop", "macbook", "máy tính", "notebook", "surface",
                             "dell", "asus", "lenovo", "acer", "gaming pc"]),
    ("Máy tính bảng",      ["ipad", "tablet", "máy tính bảng", "galaxy tab"]),
    ("Phụ kiện",           ["ốp lưng", "sạc", "cáp", "tai nghe", "airpods", "phụ kiện",
                             "case", "charger", "chuột", "bàn phím", "earphone"]),
    ("Tivi / Màn hình",    ["tivi", " tv ", "smart tv", "màn hình", "monitor", "oled", "qled"]),
    ("Điện máy gia dụng",  ["tủ lạnh", "máy giặt", "điều hoà", "máy lạnh", "lò vi sóng",
                             "nồi cơm", "bếp điện", "quạt điện", "máy hút bụi"]),
    ("Review / So sánh",   ["review", "so sánh", "đánh giá", "có nên mua", "tốt nhất",
                             "nên chọn", "nên mua", "so sanh", "danh gia", "comparison"]),
    ("Giá / Mua sắm",      ["giá", "mua", "bán", "khuyến mãi", "giảm giá", "trả góp",
                             "giá tốt", "deal", "ưu đãi", "flash sale", "price", "buy", "cheap"]),
    ("Sửa chữa / Hỗ trợ", ["sửa", "thay màn", "thay pin", "lỗi", "không lên nguồn",
                             "hư", "bị vỡ", "cách fix", "khắc phục", "repair", "fix"]),
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
            results[domain] = analyze_domain(domain, location_code, language_code)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except requests.exceptions.Timeout:
            results[domain] = {"error": f"Timeout khi phân tích {domain}. Thử lại sau."}
        except Exception as exc:
            results[domain] = {"error": str(exc)}

    return jsonify(results)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "credentials": bool(DFS_EMAIL and DFS_PASSWORD)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n🔭 RivalScope đang chạy → http://localhost:{port}\n")
    app.run(host="0.0.0.0", debug=True, port=port)
