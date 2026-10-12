# Video Grayscale Transformer

A full-stack application that transforms MP4 videos to grayscale. Upload a video of any length — the backend automatically splits it into service-compliant chunks, transforms each one, and reassembles the result into a single full-length grayscale video.

## Architecture

```
Browser (React)                    FastAPI Backend                   Commure Transform Service
       |                                  |                                     |
       |-- POST /api/transform ---------->|                                     |
       |   (upload full video)            |                                     |
       |                                  |-- FFmpeg: split into <=9.5s chunks  |
       |                                  |                                     |
       |                                  |-- POST /api/transform (chunk 1) --->|
       |                                  |<-- grayscale chunk 1 --------------|
       |                                  |-- POST /api/transform (chunk 2) --->|
       |                                  |<-- grayscale chunk 2 --------------|
       |                                  |-- ...                               |
       |                                  |                                     |
       |                                  |-- FFmpeg: concat all chunks         |
       |<-- full grayscale video ---------|                                     |
```

## Tech Stack

- **Frontend**: React + TypeScript (Vite)
- **Backend**: Python FastAPI
- **Video Processing**: FFmpeg (via `imageio-ffmpeg`)
- **HTTP Client**: httpx (async)

## Prerequisites

- Python 3.10+
- Node.js 18+
- FFmpeg (auto-installed via `imageio-ffmpeg` pip package)

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/ashnuta/Commure.git
cd Commure
```

### 2. Backend

```bash
cd backend
pip install -r requirements.txt
```

Create a `.env` file in the `backend/` directory:

```env
BASE_URL=https://interview-studio-lime.vercel.app
TRANSFORM_SERVICE_TOKEN=<your-token-here>
MAX_CLIP_BYTES=3000000
MAX_CLIP_SECONDS=10.0
```

Start the server:

```bash
uvicorn main:app --reload --port 8000
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

The app will be available at **http://localhost:5173**.

## API Endpoints

| Method | Path             | Description                                          |
|--------|------------------|------------------------------------------------------|
| GET    | `/api/health`    | Health check                                         |
| GET    | `/api/sample`    | Download the sample `short.mp4` from the service     |
| POST   | `/api/transform` | Upload an MP4, returns the grayscale-transformed MP4 |

### POST `/api/transform`

**Request**: `multipart/form-data` with a `clip` field containing an MP4 file.

**Response**: `video/mp4` — the full-length grayscale video.

**Processing pipeline** (for files exceeding service limits):
1. Split the input into segments (each < 10 seconds and < 3 MB)
2. Send each segment to the Commure transform service
3. Concatenate all grayscale segments back into one video
4. Return the reassembled result

## Service Constraints

The upstream Commure transform service enforces these limits per request:

| Constraint       | Limit         |
|------------------|---------------|
| Max duration     | 10 seconds    |
| Max file size    | 3 MB (3,000,000 bytes) |
| Format           | MP4 only      |
| Output           | Grayscale MP4 without audio |

## Error Codes

| Status | Meaning                                      |
|--------|----------------------------------------------|
| 400    | Invalid request (e.g. no clip file attached)  |
| 401    | Invalid or missing authentication token       |
| 403    | Session not live                              |
| 413    | Clip exceeds the 3 MB size limit              |
| 422    | Unsupported video format (not a valid MP4)    |
| 429    | Rate limited — temporary, retry later         |
| 503    | Service unavailable — temporary, retry later  |

The backend automatically retries on `429` and `503` responses (up to 2 retries with exponential backoff).

## Project Structure

```
Commure/
├── backend/
│   ├── main.py              # FastAPI application
│   ├── requirements.txt     # Python dependencies
│   └── .env                 # Configuration (not committed)
├── frontend/
│   ├── src/
│   │   ├── App.tsx          # Main React component
│   │   ├── App.css          # Styles
│   │   └── main.tsx         # Entry point
│   ├── vite.config.ts       # Vite config with API proxy
│   ├── index.html
│   └── package.json
└── .gitignore
```
