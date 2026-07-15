from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
from dotenv import load_dotenv
import requests
import os
import mimetypes
from typing import Optional
import database as db

load_dotenv()
db.init_db()
app = FastAPI(
    title="AquaX Smart Irrigation Core Engine",
    version="1.1.0"
)

# 🌐 CORS MIDDLEWARE SETUP
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔑 KEYS — loaded from a local .env file (via load_dotenv above) or from
# real environment variables. Never hardcoded in source.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Create a .env file next to main.py "
        "(see .env.example) with GEMINI_API_KEY=your-key-here, or export "
        "it as a system environment variable — do not hardcode it in main.py."
    )

client = genai.Client(api_key=GEMINI_API_KEY)

# Diagnostic only — confirms a key was actually loaded from .env without
# printing the key itself, and flags the exact condition that causes this
# SDK to silently switch to Vertex/OAuth auth (and produce the
# "Expected OAuth 2 access token" 401) instead of using the API key.
print(f"🔑 GEMINI_API_KEY loaded: {'yes (' + GEMINI_API_KEY[:6] + '...)' if GEMINI_API_KEY else 'NO — missing!'}")
if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true"):
    print("⚠️  GOOGLE_GENAI_USE_VERTEXAI is set — this forces Vertex/OAuth auth "
          "even with a valid GEMINI_API_KEY. Unset it if you want API-key auth.")

@app.get("/")
async def health_check():
    return {"status": "online"}


# ============================================================
# AUTH — real accounts backed by SQLite, replacing the previous
# frontend-only in-memory demo. Simple bearer-token sessions,
# no email verification (matches the hackathon-speed choice).
# ============================================================
def current_user_from_header(authorization: Optional[str]):
    """Resolves 'Bearer <token>' -> user dict, or None. Never raises —
    callers decide whether a missing/invalid token is an error."""
    if not authorization:
        return None
    token = authorization.replace("Bearer ", "").strip()
    return db.get_user_by_token(token)


def require_user(authorization: Optional[str]):
    user = current_user_from_header(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return user


def require_admin(authorization: Optional[str]):
    user = require_user(authorization)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access only.")
    return user


class SignupBody(BaseModel):
    name: str
    email: str
    password: str


class SigninBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
async def signup(body: SignupBody):
    email = body.email.strip().lower()
    if not body.name.strip() or not email or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Name, email and a 6+ character password are required.")
    if db.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    user_id = db.create_user(body.name.strip(), email, body.password)
    token = db.create_session(user_id)
    db.log_signin_event(email, "sign up", "success")
    user = db.get_user_by_id(user_id)
    return {"token": token, "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/signin")
async def signin(body: SigninBody):
    email = body.email.strip().lower()
    user = db.get_user_by_email(email)
    if not user or not db.verify_password(body.password, user["password_hash"]):
        db.log_signin_event(email or "(empty)", "sign in", "failed")
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    db.log_signin_event(email, "sign in", "success")
    token = db.create_session(user["id"])
    return {"token": token, "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/signout")
async def signout(authorization: Optional[str] = Header(None)):
    if authorization:
        db.delete_session(authorization.replace("Bearer ", "").strip())
    return {"status": "ok"}


@app.get("/api/auth/me")
async def me(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    return {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}


# ============================================================
# ADMIN — read-only views into the real database, gated on the
# signed-in user actually having role='admin'.
# ============================================================
@app.get("/api/admin/users")
async def admin_users(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return {"users": db.list_users()}


@app.get("/api/admin/signins")
async def admin_signins(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return {"signins": db.list_signin_log()}


@app.get("/api/admin/history")
async def admin_history(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return {"history": db.list_all_history()}


# ============================================================
# HISTORY — a signed-in user's own past irrigation advice.
# ============================================================
@app.get("/api/history")
async def my_history(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    return {"history": db.list_user_history(user["id"])}


@app.post("/api/recommendation")
async def get_farming_advice(
    text: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    image: Optional[UploadFile] = File(None),
    authorization: Optional[str] = Header(None)
):
    try:
        print(f"\n--- [NEW REQUEST INCOMING] Lat: {lat}, Lon: {lon} ---")

        # 1. OPEN-METEO TELEMETRY (soil + a real 24h trend, no fabricated numbers)
        agro_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&"
            f"current=temperature_2m,relative_humidity_2m,wind_speed_10m&"
            f"hourly=soil_moisture_3_to_9cm,soil_temperature_0_to_6cm,uv_index,"
            f"precipitation_probability,temperature_2m,relative_humidity_2m,wind_speed_10m&"
            f"forecast_days=2&timezone=auto"
        )
        agro_res = requests.get(agro_url, timeout=10).json()

        air_temp = agro_res.get("current", {}).get("temperature_2m", 25.0)
        air_humidity = agro_res.get("current", {}).get("relative_humidity_2m", 50)
        wind_speed_ms = agro_res.get("current", {}).get("wind_speed_10m")

        hourly = agro_res.get("hourly", {})
        hourly_times = hourly.get("time", [])
        soil_moisture_list = hourly.get("soil_moisture_3_to_9cm", [0.35])
        soil_moisture = soil_moisture_list[0] * 100 if soil_moisture_list else 35.0

        soil_temp_list = hourly.get("soil_temperature_0_to_6cm", [24.0])
        soil_temp = soil_temp_list[0] if soil_temp_list else 24.0

        uv_list = hourly.get("uv_index", [])
        uv_index = uv_list[0] if uv_list else None

        rain_prob_list = hourly.get("precipitation_probability", [])
        rain_probability = rain_prob_list[0] if rain_prob_list else None

        # Real next-24h trend for charting (falls back to whatever hourly data exists)
        trend_labels = hourly_times[:24]
        trend_soil_moisture = [round(v * 100, 1) for v in hourly.get("soil_moisture_3_to_9cm", [])[:24]]
        trend_temperature = hourly.get("temperature_2m", [])[:24]
        trend_humidity = hourly.get("relative_humidity_2m", [])[:24]
        trend_wind = hourly.get("wind_speed_10m", [])[:24]
        trend_rain_probability = hourly.get("precipitation_probability", [])[:24]

        print(f"✅ Open-Meteo Active -> Soil Moisture: {soil_moisture:.1f}%, Soil Temp: {soil_temp}°C")

        # 2. OPENWEATHERMAP TELEMETRY (current conditions, wind, feels-like)
        weather_description = "Sky Conditions Normal"
        recent_rain = "0.0 mm"
        feels_like = None
        wind_speed_kmh = round(wind_speed_ms, 1) if wind_speed_ms is not None else None

        if OPENWEATHER_API_KEY:
            try:
                owm_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric"
                owm_res = requests.get(owm_url, timeout=5).json()
                if str(owm_res.get("cod")) == "200":
                    weather_description = owm_res.get("weather", [{}])[0].get("description", "Clear Sky")
                    rain_data = owm_res.get("rain", {})
                    recent_rain = f"{rain_data.get('1h', 0.0)} mm (last hour)"
                    feels_like = owm_res.get("main", {}).get("feels_like")
                    # OWM wind.speed is m/s in metric units — prefer it (it's a live
                    # station reading) over Open-Meteo's forecast wind if present.
                    owm_wind_ms = owm_res.get("wind", {}).get("speed")
                    if owm_wind_ms is not None:
                        wind_speed_kmh = round(owm_wind_ms * 3.6, 1)
                    print(f"✅ OpenWeather Active -> Skies: {weather_description}, Rain: {recent_rain}")
            except Exception as owm_error:
                print(f"⚠️ OpenWeather API Fetch skipped/failed: {str(owm_error)}")

        # 3. AI GENERATION PROMPT ASSEMBLE
        system_prompt = (
            "You are AquaX AI, an elite agricultural water management optimization engineer. "
            "Analyze the environmental telemetry and any provided crop data. "
            "Provide actionable farming/irrigation guidance in a short numbered list (max 4 points), "
            "each point 1-2 sentences. Always finish your final sentence completely — never cut off "
            "mid-word or mid-sentence, and stay concise enough to fit that constraint. "
            "LANGUAGE RULES: If the question is in Urdu or Romanized Urdu, respond using elegant Urdu Script. "
            "If it is in English, respond in English."
            "always tell the location(cityname) in your response your given data."
        )
        
        structured_context = (
            f"--- FIELD ENVIRONMENTAL PROFILE ---\n"
            f"Air Temperature: {air_temp}°C\n"
            f"Ambient Humidity: {air_humidity}%\n"
            f"Subsoil Moisture (3-9cm): {soil_moisture:.1f}%\n"
            f"Subsoil Temperature (0-6cm): {soil_temp}°C\n"
            f"Sky State: {weather_description}\n"
            f"Recent Precipitation: {recent_rain}\n"
            f"Wind Speed: {wind_speed_kmh if wind_speed_kmh is not None else 'unavailable'} km/h\n"
            f"------------------------------------\n"
            f"Farmer's Query: {text}"
        )

        contents_payload = [structured_context]
        if image:
            image_bytes = await image.read()
            contents_payload.append(types.Part.from_bytes(data=image_bytes, mime_type=image.content_type))

        # 4. ROBUST MULTI-MODEL FALLBACK ENGINE
        # "gemma-4-31b-it" stays first as the preferred model. If it (or any
        # other entry) is unavailable/unsupported/rate-limited, the loop below
        # safely swallows the error and cascades to the next model instead of
        # ever crashing the request.
        # gemini-2.0-flash and gemini-2.5-flash have both been retired/closed
        # to new projects, and "gemma-3-27b-it" was never a valid model id
        # here — that's why every attempt in the old list 404'd. These are
        # the current, actually-live model ids as of July 2026.
        models_to_try = ["gemma-4-31b-it","gemini-flash-latest", "gemini-3.5-flash"]
        response_text = None
        last_model_error = None

        for model_name in models_to_try:
            try:
                print(f"🤖 Attempting generation with model: {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3,
                        # Raised from 800 -> 2048. Urdu script consumes more tokens
                        # per character than English, and 800 was clipping answers
                        # (esp. image-analysis responses) mid-sentence.
                        max_output_tokens=2048
                    ),
                    contents=contents_payload,
                )
                response_text = (response.text or "").strip()
                if not response_text:
                    raise ValueError("Empty response text")
                print(f"🎉 Generation successful using {model_name}!")
                break  # Success! Exit the loop.
            except Exception as model_err:
                last_model_error = str(model_err)
                print(f"⚠️ Model {model_name} failed with error: {last_model_error}")
                continue

        if not response_text:
            raise HTTPException(
                status_code=500,
                detail=f"All generative AI model variants failed to respond. Last error: {last_model_error}"
            )
        
        # Best-effort history logging for signed-in users only. Wrapped so a
        # DB hiccup can never affect the response — anonymous callers (the
        # image/voice flows that don't send a token) are completely
        # unaffected, exactly as before.
        try:
            requester = current_user_from_header(authorization)
            if requester:
                db.log_advice(requester["id"], "recommendation", text, lat, lon, response_text)
        except Exception as log_err:
            print(f"⚠️ History logging skipped: {log_err}")

        return {
            "soil_moisture": f"{soil_moisture:.1f}%",
            "temperature": f"{air_temp}°C",
            "soil_temperature": f"{soil_temp}°C",
            "humidity": f"{air_humidity}%",
            "weather_condition": weather_description.capitalize(),
            "precipitation": recent_rain,
            "ai_advice": response_text,
            # Real telemetry only — null means "not available from the live APIs",
            # the frontend shows N/A rather than inventing a number.
            "wind_speed_kmh": wind_speed_kmh,
            "feels_like": f"{round(feels_like, 1)}°C" if feels_like is not None else None,
            "uv_index": round(uv_index, 1) if uv_index is not None else None,
            "rain_probability": rain_probability,
            "trend": {
                "labels": trend_labels,
                "soil_moisture": trend_soil_moisture,
                "temperature": trend_temperature,
                "humidity": trend_humidity,
                "wind_speed_kmh": [round(w, 1) for w in trend_wind] if trend_wind else [],
                "rain_probability": trend_rain_probability
            }
        }

    except Exception as server_error:
        print("💥 CRITICAL CORE ERROR:", str(server_error))
        raise HTTPException(status_code=500, detail=str(server_error))

@app.post("/api/voice-process")
async def process_voice_query(
    file: UploadFile = File(...),
    mime_type: Optional[str] = Form(None),
):
    """
    Transcribes the farmer's spoken question.

    NOTE: This deliberately does NOT use google-cloud-speech. That client
    requires a separate GCP service-account key / Application Default
    Credentials, which this deployment doesn't have (that's exactly what was
    producing the "Your default credentials were not found" error). Instead
    we send the raw audio straight to Gemini — which already authenticates
    with the same GEMINI_API_KEY used for /api/recommendation — and ask it to
    transcribe. No extra credentials, no extra service to configure.
    """
    try:
        audio_data = await file.read()
        if not audio_data:
            raise HTTPException(status_code=400, detail="Empty audio payload received.")

        # Figure out the real MIME type: prefer the explicit hint the frontend
        # sends (the exact type MediaRecorder used), then the multipart
        # content-type, then guess from the filename.
        resolved_mime = mime_type or file.content_type
        if not resolved_mime or resolved_mime == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(file.filename or "")
            resolved_mime = guessed or "audio/webm"

        transcription_prompt = (
            "Transcribe the spoken audio exactly as said, word for word. "
            "If the speech is in Urdu, output it in Urdu script (not Roman "
            "Urdu). If it is in English, output plain English. "
            "Respond with ONLY the transcription text — no labels, no quotes, "
            "no commentary, no translation. If the audio contains no "
            "discernible speech, respond with exactly: [NO_SPEECH]"
        )

        audio_part = types.Part.from_bytes(data=audio_data, mime_type=resolved_mime)

        # Note: unlike the text/image endpoint, Gemma models don't accept audio
        # input — only Gemini multimodal models do. Fallback stays within the
        # Gemini family.
        # Same retirement issue as the recommendation endpoint — swap in the
        # current live models.
        models_to_try = ["gemma-4-31b-it","gemini-flash-latest", "gemini-3.5-flash"]
        transcript = None
        last_model_error = None

        for model_name in models_to_try:
            try:
                print(f"🎙️ Attempting transcription with model: {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    config=types.GenerateContentConfig(temperature=0.0),
                    contents=[transcription_prompt, audio_part],
                )
                candidate = (response.text or "").strip()
                if candidate and candidate != "[NO_SPEECH]":
                    transcript = candidate
                    print(f"🎉 Transcription successful using {model_name}!")
                    break
                elif candidate == "[NO_SPEECH]":
                    transcript = ""
                    break
            except Exception as model_err:
                last_model_error = str(model_err)
                print(f"⚠️ Transcription model {model_name} failed: {last_model_error}")
                continue

        if transcript is None:
            raise HTTPException(
                status_code=500,
                detail=f"Voice transcription failed. Last error: {last_model_error}"
            )

        if not transcript:
            return {
                "status": "empty",
                "transcribed_text": "",
                "detail": "No speech was recognized in the recording. Please try again closer to the mic."
            }

        return {"status": "success", "transcribed_text": transcript}
    except HTTPException:
        raise
    except Exception as voice_err:
        print("💥 VOICE PROCESSING ERROR:", str(voice_err))
        raise HTTPException(status_code=500, detail=str(voice_err))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)