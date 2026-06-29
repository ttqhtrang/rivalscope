# RivalScope — Phân Tích Traffic Đối Thủ Cạnh Tranh

**Live:** https://rivalscope-pq4q.onrender.com/

## Cách chạy local

```bash
pip install -r requirements.txt
cp .env.example .env   # điền credentials DataForSEO
python3 app.py
```

Mở http://localhost:5050

## Deploy (Render)

Repo có `render.yaml` — connect GitHub trên render.com là tự động deploy.

Cần set 2 env vars trên Render dashboard:
- `DATAFORSEO_EMAIL`
- `DATAFORSEO_PASSWORD`
