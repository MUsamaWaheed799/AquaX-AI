# 🌾 AquaX AI — Smart Irrigation Assistant

A web app that tells farmers when and how much to water their crops — using **live weather/soil data** and **Google Gemini AI**, with support for text, voice, and photo queries in English and Urdu.

---

## 1. What is it?

AquaX AI is a full-stack irrigation advisory tool. A farmer can ask a question by **typing, speaking, or uploading a photo** of their crop, and the app replies with clear, actionable irrigation advice based on real environmental data — not guesswork.

## 2. The problem it solves

Farmers often over-water or under-water crops because they rely on experience and intuition rather than data. AquaX AI pulls **real-time weather and soil data** for the farmer's exact location and combines it with AI reasoning to give a data-backed recommendation instead of a guess.

## 3. How it works

1. **Farmer asks a question** — by text, voice recording, or photo — and shares their location.
2. **The backend fetches real data**:
   - Soil moisture & soil temperature — [Open-Meteo](https://open-meteo.com/)
   - Air temperature, humidity, wind, rain chance — Open-Meteo + [OpenWeatherMap](https://openweathermap.org/)
3. **All this data is packed into a prompt** and sent to Google's Gemini model, which returns a short, numbered list of practical advice (max 4 points).
4. **The answer is shown to the farmer**, alongside a 24-hour trend chart (soil moisture, temperature, humidity, wind, rain probability) rendered with Chart.js.
5. If the farmer is **signed in**, the question and answer are saved to their personal history.

## 4. Key features

| Feature | What it does |
|---|---|
| 🔑 Real login system | Signup/signin with hashed passwords (PBKDF2 + per-user salt), stored in SQLite |
| 🎤 Voice queries | Records audio → sends to Gemini → returns a transcription (no separate speech API needed) |
| 📷 Photo queries | Farmer can attach a crop photo alongside their question |
| 📊 Live trend charts | 24-hour soil/weather trend shown visually via Chart.js |
| 🌐 Bilingual replies | Detects Urdu vs English and replies in the same language |
| 🔐 Admin panel | View all registered users, sign-in activity log, and every user's advice history |
| 🛡️ Model fallback | If one AI model fails or is rate-limited, automatically retries with the next model |

## 5. Tech stack

- **Backend:** FastAPI (Python) — handles routing, auth.
- **Database:** SQLite (`aquax.db`) — stores users, sessions, sign-in logs, and advice history
- **Frontend:** Single HTML file — Tailwind CSS for styling, Chart.js for charts, vanilla JavaScript for logic
- **AI Model:** Google Gemini via the `google-genai` SDK(Sytem Development kit)
- **Weather/soil data:** Open-Meteo (free, no key required) + OpenWeatherMap (optional — adds "feels like" temp and live sky conditions)

## 6. Project structure

```
aquax/
├── main.py            # FastAPI app: auth, admin, recommendation & voice endpoints
├── database.py         # SQLite persistence layer (users, sessions, logs, history)
├── index.html          # Single-file frontend (UI + charts + API calls)
├── aquax.db            # SQLite database file (auto-created on first run)
├── requirement.txt     # Python dependencies
├── .env                # Local secrets (GEMINI_API_KEY, OPENWEATHER_API_KEY) — never commit
└── .gitignore
```

## 7. Setup

```bash
pip install -r requirement.txt
```

Create a `.env` file next to `main.py`:

```
GEMINI_API_KEY=your-key-here
OPENWEATHER_API_KEY=your-key-here   # optional
```

Run the server:

```bash
python main.py
```

The API will be live at `http://127.0.0.1:8000`. Open `index.html` in a browser to use the app.

**Default admin account** (seeded automatically on first run):
- Username: `admin`
- Password: `admin123`
> ⚠️ Change this password after first login.

## 8. What makes this project solid

- **No fabricated data** — if a data point (e.g. wind speed) isn't available from the live API, the frontend shows "N/A" rather than inventing a number.
- **Fails gracefully** — if OpenWeatherMap or one AI model is down, the app keeps working via fallbacks.
- **Lightweight backend** — password hashing and DB logic use only Python's standard library (`sqlite3`, `hashlib`, `secrets`), no extra dependencies.

## 9. Known limitations / future improvements

- Sessions currently never expire — adding expiry/refresh tokens would improve security for production use.
- Admin login has no rate-limiting or lockout after repeated failed attempts.
- SQLite is fine for a demo/small deployment; a move to Postgres would help it scale to many concurrent users.
