import os
import io
import uuid
import time
import hashlib
import mimetypes
import requests
import subprocess
import tempfile
import logging
from spitch import Spitch
from pydub import AudioSegment
from twilio.rest import Client

# Initialize logging for utils.py
logger = logging.getLogger(__name__)

# Load API keys and clients
SPITCH_API_KEY = os.getenv('SPITCH_API_KEY') # Fallback for local testing
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPITCH_CLIENT = Spitch(api_key=SPITCH_API_KEY) 
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

# Only import genai if GEMINI_API_KEY is available and we're not running in a very restricted environment
# This helps avoid ImportError if the user doesn't need Gemini or hasn't installed its library.
try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    else:
        logger.warning("GEMINI_API_KEY not set. Gemini functions may not work.")
except ImportError:
    logger.warning("google-generativeai library not installed. Gemini functions will not work.")
    genai = None # Set to None if import fails

def upload_to_cloudinary(file_obj):
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    if not (cloud_name and api_key and api_secret):
        print("❌ Missing Cloudinary credentials.")
        return None

    upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload" # Use video for audio streaming

    timestamp = int(time.time())

    params_to_sign = f"timestamp={timestamp}{api_secret}"
    signature = hashlib.sha1(params_to_sign.encode('utf-8')).hexdigest()

    data = {
        "api_key": api_key,
        "timestamp": timestamp,
        "signature": signature
    }

    if isinstance(file_obj, str):
        filename = os.path.basename(file_obj)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"file": (filename, open(file_obj, "rb"), content_type)}
    else:
        files = {"file": ("output.mp3", file_obj, "audio/mpeg")}

    try:
        response = requests.post(upload_url, data=data, files=files)

        if response.status_code == 200:
            return response.json().get("secure_url")
        else:
            logger.error(f"Cloudinary upload error: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Cloudinary upload exception: {e}")
        return None

def ask_gemini(prompt, lang):
    if not GEMINI_API_KEY:
        return "I'm sorry, Gemini is not configured. Please set GEMINI_API_KEY."
    if not genai:
        return "I'm sorry, the Google Generative AI library is not installed."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

    # Friendly assistant personality
    if lang == 'undefined':
        system_instruction = {
            "role": "user",
            "parts": [{
                "text": (
                    f"You're a friendly multilingual Health, Education, Finance and Entertainment assistant named VoiceBridge who explains things clearly, simply, and respectfully. "
                    f"Always answer like you're speaking directly to the person, not writing a formal essay. Don't try to format text in anyway(use of double asterisk before and after words to makde them bold, em dahses), just return plain text"
                    f"Be short but descriptive, helpful, and human. You MUST detect the language used in {prompt} and reply in that language"
                )
            }]
        }
    else:
        language = {
            "yo": "yoruba",
            "ig": "igbo",
            "ha": "hausa"
        }.get(lang, "english")

        system_instruction = {
            "role": "user",
            "parts": [{
                "text": (
                    f"You're a friendly multilingual Health, Education, Finance and Entertainment assistant named VoiceBridge who explains things clearly, simply, and respectfully. "
                    f"Always answer like you're speaking directly to the person, not writing a formal essay. Don't try to format text in anyway(use of double asterisk before and after words to makde them bold, em dahses), just return plain text"
                    f"Be short but descriptive, helpful, and human. You MUST reply in this language: {language}."
                )
            }]
        }

    user_message = {
        "role": "user",
        "parts": [{"text": prompt}]
    }

    body = {
        "contents": [system_instruction, user_message]
    }

    try:
        response = requests.post(url, json=body)
        response.raise_for_status()
        return response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    except Exception as e:
        logger.error("Gemini API failed: %s", e)
        return "I'm sorry, I couldn't understand your request."


def normalize_audio(audio_bytes, input_format):
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format=input_format)
        audio = audio.set_channels(1).set_frame_rate(16000)
        out = io.BytesIO()
        audio.export(out, format="wav")
        return out.getvalue()
    except Exception as e:
        logger.error("Failed to convert %s to wav: %s", input_format, e)
        return None

def safe_tts(text, language, prefix):
    try:
        voice_map = {
            "en": "lucy",
            "yo": "sade",
            "ig": "ngozi",
            "ha": "amina"
        }
        voice_id = voice_map.get(language, "lucy")

        response = SPITCH_CLIENT.speech.generate( # Use the global SPITCH_CLIENT
            text=text,
            language=language,
            voice=voice_id
        )

        wav_path = f"/tmp/{prefix}_{uuid.uuid4().hex}.wav"
        with open(wav_path, "wb") as f:
            f.write(response.read())

        mp3_path = wav_path.replace(".wav", ".mp3")
        AudioSegment.from_wav(wav_path).export(mp3_path, format="mp3")

        return upload_to_cloudinary(mp3_path)

    except Exception as e:
        logger.error("Spitch TTS failed: %s", e)
        return None


def safe_stt(audio_bytes, language):
    try:
        usable_audio = normalize_audio(audio_bytes, input_format='webm')
        if not usable_audio:
            logger.error("Normalization gave None")
            return None

        response = SPITCH_CLIENT.speech.transcribe(language=language, content=usable_audio) # Use global SPITCH_CLIENT
        transcript = response.text
        if language != "en":
            translation = SPITCH_CLIENT.text.translate(text=transcript, source=language, target="en") # Use global SPITCH_CLIENT
            transcript = translation.text
        return transcript
    except Exception as e:
        logger.error("Spitch STT failed: %s", e)
        return None

def safe_gemini_conversational_audio_or_text(audio_bytes=None, input_format=None, text_input=None):
    if not GEMINI_API_KEY or not genai:
        logger.error("Gemini is not configured or its library is not installed.")
        return "I'm sorry, Gemini is not available.", "en"
    
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        prompt_parts = []
        
        base_instructions = (
            "Please analyze the provided input. First, identify the language used. "
            "Then, respond naturally and conversationally to the content in the *exact same language* you detected. "
            "Finally, append a language code at the very end of your response, formatted as 'Language Code: [code]'. "
            "Use these specific codes: 'yo' for Yoruba, 'ig' for Igbo, 'ha' for Hausa, 'en' for English. "
            "If the language is not Yoruba, Igbo, Hausa, or English, default the language code to 'en'. "
            "Your conversational response should precede the language code. "
            "You're a friendly multilingual Health, Education, Finance and Entertainment and AI assistant generally named VoiceBridge who explains things clearly, simply, and respectfully. "
            "Always answer like you're speaking directly to the person, not writing a formal essay. Don't try to format text in anyway (use of double asterisk before and after words to make them bold, em dashes), just return plain text"
        )

        if text_input:
            logger.info("Processing text input with Gemini 2.0 Flash.")
            prompt_parts.append(text_input)
            prompt_parts.append(base_instructions)
        elif audio_bytes:
            logger.info("Processing audio input with Gemini 2.0 Flash.")
            usable_audio_wav = normalize_audio(audio_bytes, input_format=input_format)
            if not usable_audio_wav:
                logger.error("Audio normalization failed using existing normalize_audio function.")
                return None, "en"

            audio_file = {
                "mime_type": "audio/wav",
                "data": usable_audio_wav
            }
            prompt_parts.append(audio_file)
            prompt_parts.append(base_instructions)
        else:
            logger.error("No audio bytes or text input provided to safe_gemini_conversational_audio_or_text.")
            return None, "en"

        response = model.generate_content(prompt_parts)
        full_gemini_response = response.text.strip()
        logger.info(f"Gemini raw response: {full_gemini_response}")

        lang_code_prefix = "Language Code:"
        conversational_response = None
        detected_lang = "en" 

        if lang_code_prefix in full_gemini_response:
            parts = full_gemini_response.rsplit(lang_code_prefix, 1)
            conversational_response = parts[0].strip()
            code_raw = parts[1].strip().lower()

            if code_raw in ["yo", "ig", "ha", "en"]:
                detected_lang = code_raw
            else:
                detected_lang = "en"
        else:
            logger.warning("Gemini response did not contain the expected language code format. Defaulting to 'en'.")
            conversational_response = full_gemini_response

        if not conversational_response:
            logger.error("Gemini failed to provide a conversational response.")
            return None, "en"

        return conversational_response, detected_lang

    except Exception as e:
        logger.error(f"safe_gemini_conversational_audio_or_text failed: {e}")
        return None, "en"

def send_whatsapp_audio(to_number, media_url, text="Here's your answer 🎧"):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        logger.error("Twilio credentials not set. Cannot send WhatsApp audio.")
        return None
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # Ensure both numbers are in WhatsApp format
        from_whatsapp = TWILIO_WHATSAPP_NUMBER
        to_whatsapp = to_number
        
        # Add 'whatsapp:' prefix if not already present
        if not from_whatsapp.startswith('whatsapp:'):
            from_whatsapp = f'whatsapp:{from_whatsapp}'
        if not to_whatsapp.startswith('whatsapp:'):
            to_whatsapp = f'whatsapp:{to_whatsapp}'
        
        message = twilio_client.messages.create(
            from_=from_whatsapp,
            to=to_whatsapp,
            body=text,
            media_url=[media_url]
        )
        logger.info(f"✅ WhatsApp audio sent: {message.sid}")
        return message.sid
    except Exception as e:
        logger.error(f"Failed to send WhatsApp audio: {e}")
        return None

def send_whatsapp_message(to_number, text_message):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        logger.error("Twilio credentials not set. Cannot send WhatsApp message.")
        return None
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # Ensure both numbers are in WhatsApp format
        from_whatsapp = TWILIO_WHATSAPP_NUMBER
        to_whatsapp = to_number
        
        # Add 'whatsapp:' prefix if not already present
        if not from_whatsapp.startswith('whatsapp:'):
            from_whatsapp = f'whatsapp:{from_whatsapp}'
        if not to_whatsapp.startswith('whatsapp:'):
            to_whatsapp = f'whatsapp:{to_whatsapp}'
        
        message = twilio_client.messages.create(
            from_=from_whatsapp,
            to=to_whatsapp,
            body=text_message
        )
        logger.info(f"✅ WhatsApp text sent: {message.sid}")
        return message.sid
    except Exception as e:
        logger.error(f"Failed to send WhatsApp text message: {e}")
        return None