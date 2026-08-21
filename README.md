# API Sentinel

AI-generated edge-case API tests + automated performance regression detection, built on FastAPI, Locust, and LLaMA-3.

## Problem

When a backend API changes, two things break silently:
1. **Functional edge cases** — null values, boundary numbers, malformed payloads, wrong types — get missed because writing exhaustive edge-case tests by hand is slow and skipped under deadline pressure.
2. **Performance regressions** — an endpoint gets slower after a change, and nobody notices until it's in production, because latency isn't tracked run-over-run.

## What it does

- Takes an API's OpenAPI/Swagger spec as input
- Uses an LLM (LLaMA-3 via Groq) to auto-generate edge-case test scenarios per endpoint
- Runs concurrent load tests against the live API using Locust, capturing p50/p95/p99 latency
- Stores every run in MongoDB and compares it against historical runs
- Flags functional failures and performance regressions (latency degradation beyond a set threshold)
- Surfaces everything on a Streamlit dashboard

## Architecture

```
OpenAPI Spec
     │
     ▼
[Schema Parser] → extracts endpoint definitions
     │
     ▼
[LLM Test Generator] → Groq/LLaMA-3 generates edge-case inputs
     │
     ▼
[Locust Load Runner] → fires tests concurrently, captures latency
     │
     ▼
[MongoDB] → stores run results with timestamp
     │
     ▼
[Regression Detector] → diffs current run vs. historical runs
     │
     ▼
[Streamlit Dashboard] → pass/fail status + latency trends + flags
```

Entire flow is orchestrated via a FastAPI endpoint: `POST /run-test` with a target spec URL.

## Tech Stack

| Component | Tech |
|---|---|
| Orchestration API | FastAPI |
| Test generation | Groq API (LLaMA-3) |
| Load testing | Locust |
| Storage | MongoDB Atlas |
| Dashboard | Streamlit |
| Deployment | Render / Railway |

## Project Structure

```
api-sentinel/
├── app/
│   ├── main.py                 # FastAPI orchestration endpoint
│   ├── config.py
│   ├── generator/
│   │   ├── llm_client.py        # Groq API wrapper
│   │   ├── prompts.py           # test-generation prompt templates
│   │   └── schema_parser.py     # parses OpenAPI spec into endpoint schemas
│   ├── loadtest/
│   │   ├── locustfile.py        # Locust user classes
│   │   └── runner.py            # triggers Locust programmatically
│   ├── db/
│   │   ├── models.py            # Pydantic models for run/result docs
│   │   └── mongo_client.py
│   ├── regression/
│   │   └── detector.py          # compares current run vs. historical runs
│   └── dashboard/
│       └── streamlit_app.py
├── tests/                       # tests for this tool itself
├── target_api/                  # demo target API (Adaptive Diagnostic Engine)
├── docs/
│   ├── architecture.png
│   ├── demo.gif
│   └── problem_statement.md
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Setup

```bash
# clone
git clone https://github.com/<your-username>/api-sentinel.git
cd api-sentinel

# install dependencies
pip install -r requirements.txt

# configure environment
cp .env.example .env
# fill in: GROQ_API_KEY, MONGODB_URI

# run the FastAPI orchestrator
uvicorn app.main:app --reload

# run the dashboard
streamlit run app/dashboard/streamlit_app.py
```

## Usage

```bash
curl -X POST http://localhost:8000/run-test \
  -H "Content-Type: application/json" \
  -d '{"spec_url": "http://localhost:9000/openapi.json"}'
```

Open the Streamlit dashboard to view results, latency trends, and flagged regressions.

## Demo

[GIF / dashboard link here]

## Results

- **Bug caught:** [fill in — specific edge case the LLM found that manual tests missed]
- **Regression caught:** [fill in — before/after latency numbers on an intentionally slowed endpoint]

## Limitations & Future Work

- No CI/CD integration yet (GitHub Actions webhook trigger planned)
- REST/OpenAPI only — no GraphQL support
- Basic injection-style edge cases only — not a full security/pentesting tool

## License

MIT