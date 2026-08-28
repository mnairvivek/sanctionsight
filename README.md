# Sanctions Site Search Tool — React + FastAPI Frontend

## Project Structure

```
sanctions-tool/
├── backend/
│   ├── main.py                 # FastAPI wrapper (calls your existing engine)
│   ├── sanctions_engine.py     # YOUR EXISTING CODE — drop it here unchanged
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Header.jsx
│   │   │   ├── InputForm.jsx
│   │   │   ├── ProgressView.jsx
│   │   │   ├── Dashboard.jsx
│   │   │   ├── VerdictCard.jsx
│   │   │   ├── RiskCharts.jsx
│   │   │   └── ResultsTable.jsx
│   │   ├── App.jsx
│   │   ├── main.jsx
│   │   └── index.css
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   ├── tailwind.config.js
│   └── postcss.config.js
└── README.md
```

## Setup

### 1. Backend

```bash
cd backend

# Copy your existing sanctions tool script here as sanctions_engine.py
cp /path/to/your/sanctions_search_tool.py ./sanctions_engine.py

# Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_lg

# Set API keys
export GOOGLE_API_KEY="your_google_api_key"
export GOOGLE_CSE_ID="your_search_engine_id"
export GOOGLE_GENAI_API_KEY="your_genai_key"  # optional, for Gemma 4 verdict

# Start the API server
uvicorn main:app --reload --port 8000
```

### 2. Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start dev server (proxies /api to localhost:8000)
npm run dev
```

Open **http://localhost:3000** in your browser.

## How It Works

1. **Your existing Python code is 100% unchanged.** It lives as `sanctions_engine.py` in the backend folder.

2. **FastAPI (`main.py`)** wraps it with a thin REST API:
   - `POST /api/analyze` — starts a background analysis job
   - `GET /api/stream/{job_id}` — SSE endpoint for real-time progress
   - `GET /api/result/{job_id}` — fetch completed results
   - `GET /api/health` — check API key status

3. **React frontend** provides:
   - Clean input form with quick-scan toggle and name co-occurrence config
   - Real-time progress with SSE streaming (jurisdiction-by-jurisdiction)
   - Dashboard with stat cards, risk distribution donut chart, jurisdiction bar chart
   - Gemma 4 verdict card with risk factors and recommendations
   - Sortable, searchable, filterable results table with expandable rows
   - Social media profile links
   - Fully responsive (mobile/tablet/desktop)

## Production Build

```bash
cd frontend
npm run build
# Serve the dist/ folder with the FastAPI backend or a CDN
```

## Deploying to Google Cloud Run

```bash
# Build a Docker image combining both backend and frontend
# (Dockerfile not included yet — add in Phase 6)
gcloud run deploy sanctions-search \
  --source . \
  --region us-central1 \
  --set-env-vars GOOGLE_API_KEY=xxx,GOOGLE_CSE_ID=yyy
```
