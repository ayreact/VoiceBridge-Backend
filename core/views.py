import os
import io
import logging
import requests # Still needed for IVR to fetch recording

from django.contrib.auth.models import User
from rest_framework import generics, status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.pagination import PageNumberPagination
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.http import HttpResponse, JsonResponse
from twilio.twiml.messaging_response import MessagingResponse
from django.views import View

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

from .models import UserProfile, QueryHistory, LessonContent
from .serializers import (
    UserSerializer,
    UserProfileSerializer,
    QueryHistorySerializer,
    LessonContentSerializer,
    CustomTokenObtainPairSerializer
)
from .utils import ( # Import all helper functions from utils
    upload_to_cloudinary,
    ask_gemini,
    normalize_audio,
    safe_tts,
    safe_stt,
    safe_gemini_conversational_audio_or_text,
    send_whatsapp_audio,
    send_whatsapp_message # Also added send_whatsapp_message to utils
)


logger = logging.getLogger(__name__)


# === AUTH ===
class RegisterView(generics.CreateAPIView):
    serializer_class = UserSerializer
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            "user": serializer.data,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }, status=status.HTTP_201_CREATED)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as e:
            logger.exception("🔴 Login failed")
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)

# === PROFILE ===
class UserProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileSerializer
    def get_object(self):
        return UserProfile.objects.get(user=self.request.user)

# === LOGS ===
class QueryHistoryList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = QueryHistorySerializer
    def get_queryset(self):
        return QueryHistory.objects.filter(user=self.request.user)

# === LESSONS ===
class LessonPagination(PageNumberPagination):
    page_size = 6
    page_size_query_param = 'page_size'
    max_page_size = 100

class LessonContentView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = LessonContentSerializer
    pagination_class = LessonPagination

    def get_queryset(self):
        lang = self.request.query_params.get("language")
        category = self.request.query_params.get("category")
        search_query = self.request.query_params.get("search")

        qs = LessonContent.objects.all().order_by('-created_at')

        if lang and lang != 'all':
            qs = qs.filter(language=lang)
        if category and category != 'all':
            qs = qs.filter(category=category)
        if search_query:
            qs = qs.filter(title__icontains=search_query) | \
                 qs.filter(body__icontains=search_query)

        return qs

# === TEXT ASSISTANT ===
class AssistantQueryView(views.APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        query = request.data.get("text")
        language = request.data.get("language", "en")
        category = request.data.get("category", "general")

        if not query:
            return Response({"error": "Missing query text"}, status=400)

        ai_response = ask_gemini(query, language)
        audio_url = safe_tts(ai_response, language, "assistant")

        QueryHistory.objects.create(
            user=request.user,
            query=query,
            response=ai_response,
            category=category,
            language=language,
        )

        return Response({
            "query": query,
            "response": ai_response,
            "audio_url": audio_url
        })

# === VOICE ASSISTANT ===
class VoiceUploadView(views.APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        audio = request.FILES.get("file")
        language = request.data.get("language", "en")
        if not audio:
            return Response({"error": "No audio provided"}, status=400)

        audio_bytes = audio.read()
        transcription = safe_stt(audio_bytes, language)
        if not transcription:
            return Response({"error": "STT failed"}, status=500)

        ai_response = ask_gemini(transcription, language)
        audio_url = safe_tts(ai_response, language, "voice")
        # Ensure upload_to_cloudinary can handle BytesIO object or file path
        uploaded_audio_url = upload_to_cloudinary(io.BytesIO(audio_bytes)) 

        return Response({
            "query": transcription,
            "response": ai_response,
            "audio_url": audio_url,
            "uploaded_input_audio_url": uploaded_audio_url
        })

# === IVR ===
@method_decorator(csrf_exempt, name='dispatch')
class IVRHookView(View):
    def post(self, request):
        try:
            recording_url = request.POST.get("RecordingUrl")

            if not recording_url:
                return HttpResponse("""
                    <Response>
                        <Say voice="alice">Welcome to VoiceBridge. Please speak after the beep.</Say>
                        <Record action="/api/assistant/ivr-hook" method="POST" maxLength="10" />
                    </Response>
                """, content_type="text/xml")

            audio_data = requests.get(recording_url).content

            ai_response, lang = safe_gemini_conversational_audio_or_text(audio_bytes=audio_data, input_format='wav')
            if not ai_response:
                logger.warning("STT failed or returned empty transcript for IVR.")
                return HttpResponse("""
                    <Response>
                        <Say voice="alice">Sorry, we couldn't hear you. Please try again.</Say>
                    </Response>
                """, content_type="text/xml")

            audio_url = safe_tts(ai_response, lang, "ivr")
            if not audio_url:
                logger.warning("TTS failed for IVR — no audio to play.")
                return HttpResponse("""
                    <Response>
                        <Say voice="alice">Sorry, I'm having trouble responding right now.</Say>
                    </Response>
                """, content_type="text/xml")

            twiml = f"""<Response><Play>{audio_url}</Play></Response>"""
            return HttpResponse(twiml, content_type="text/xml")

        except Exception as e:
            logger.error("❌ IVR processing failed: %s", str(e))
            return HttpResponse("""
                <Response>
                    <Say voice="alice">Sorry, something went wrong. Please try again later.</Say>
                </Response>
            """, content_type="text/xml")

# === WHATSAPP ===
@method_decorator(csrf_exempt, name='dispatch')
class WhatsAppWebhookView(View):
    def post(self, request):
        media_type = request.POST.get("MediaContentType0")
        audio_url = request.POST.get("MediaUrl0") if media_type and media_type.startswith("audio") else None
        body_text = request.POST.get("Body")
        user_phone = request.POST.get("From", "anonymous")

        ai_response = None
        lang = "en"
        audio_reply_url = None

        # Create TwiML response for acknowledgment
        twiml_response = MessagingResponse()

        if audio_url:
            try:
                logger.info(f"🎵 Processing WhatsApp audio from {audio_url}")
                
                # Download the audio file with Twilio authentication
                try:
                    audio_data = self.download_twilio_media(audio_url)
                    if not audio_data:
                        logger.error("❌ Failed to download audio with authentication")
                        send_whatsapp_message(user_phone, "Sorry, I couldn't access your audio message. Can you try again?")
                        return HttpResponse(str(twiml_response), content_type='text/xml')
                    
                    logger.info(f"✅ Downloaded audio file: {len(audio_data)} bytes")
                except Exception as download_error:
                    logger.error(f"❌ Failed to download audio: {download_error}")
                    send_whatsapp_message(user_phone, "Sorry, I couldn't download your audio message. Can you try again?")
                    return HttpResponse(str(twiml_response), content_type='text/xml')

                # Enhanced audio processing for WhatsApp OGG format
                processed_audio_data = self.process_whatsapp_audio(audio_data, 'ogg')
                if not processed_audio_data:
                    logger.error("❌ WhatsApp audio processing failed completely")
                    send_whatsapp_message(user_phone, "I couldn't process your audio format. Could you try sending a text message instead?")
                    return HttpResponse(str(twiml_response), content_type='text/xml')

                # Try processing with Gemini
                try:
                    ai_response, lang = safe_gemini_conversational_audio_or_text(
                        audio_bytes=processed_audio_data, 
                        input_format='wav'
                    )
                except Exception as gemini_error:
                    logger.error(f"❌ Gemini audio processing failed: {gemini_error}")
                    # Fallback: try with original audio data as OGG
                    try:
                        logger.info("Trying fallback with original OGG data")
                        ai_response, lang = safe_gemini_conversational_audio_or_text(
                            audio_bytes=audio_data, 
                            input_format='ogg'
                        )
                    except Exception as fallback_error:
                        logger.error(f"❌ Fallback OGG processing also failed: {fallback_error}")
                        send_whatsapp_message(user_phone, "I had trouble understanding your audio. Could you try sending a text message instead?")
                        return HttpResponse(str(twiml_response), content_type='text/xml')

                if not ai_response:
                    logger.warning("Gemini returned empty response from WhatsApp audio.")
                    send_whatsapp_message(user_phone, "I couldn't understand anything in your audio message. Could you try speaking more clearly or send a text?")
                    return HttpResponse(str(twiml_response), content_type='text/xml')

            except Exception as e:
                logger.error(f"❌ Complete WhatsApp audio processing failed: {str(e)}")
                send_whatsapp_message(user_phone, "There was an unexpected error with your audio message. Please try a shorter message or use text.")
                return HttpResponse(str(twiml_response), content_type='text/xml')

        elif body_text:
            # Text processing - always returns text that will be converted to audio
            ai_response, lang = safe_gemini_conversational_audio_or_text(text_input=body_text)
            if not ai_response:
                logger.warning("Gemini failed to generate response from WhatsApp text.")
                send_whatsapp_message(user_phone, "Sorry, I couldn't understand your text message. Can you rephrase?")
                return HttpResponse(str(twiml_response), content_type='text/xml')
            
        else:
            logger.warning("WhatsApp webhook received no audio or text input.")
            send_whatsapp_message(user_phone, "I didn't receive any message. Please send an audio or text message.")
            return HttpResponse(str(twiml_response), content_type='text/xml')

        try:
            if ai_response:
                # ALWAYS respond with audio + text, regardless of input type
                audio_reply_url = safe_tts(ai_response, lang, "wa")

                if audio_reply_url:
                    # Send audio message with text as caption
                    message_sid = send_whatsapp_audio(
                        to_number=user_phone,
                        media_url=audio_reply_url,
                        text=ai_response  # This will be the text caption
                    )
                    send_whatsapp_message(
                        to_number=user_phone,
                        text_message=ai_response
                    )
                    logger.info("✅ WhatsApp audio+text response sent with SID: %s", message_sid)
                else:
                    # If TTS fails, fall back to text-only
                    logger.warning("⚠️ TTS failed — sending text-only response")
                    message_sid = send_whatsapp_message(
                        to_number=user_phone,
                        text_message=ai_response
                    )
                    logger.info("✅ WhatsApp text-only fallback sent with SID: %s", message_sid)
            else:
                logger.warning("⚠️ AI response was empty — no message sent to WhatsApp")
                send_whatsapp_message(user_phone, "I'm sorry, I couldn't generate a response at this time. Please try again.")

            # Return empty TwiML to acknowledge receipt
            return HttpResponse(str(twiml_response), content_type='text/xml')
            
        except Exception as e:
            logger.error("❌ WhatsApp response sending failed: %s", str(e))
            send_whatsapp_message(user_phone, "An unexpected error occurred while trying to send my response. Please try again later.")
            return HttpResponse(str(twiml_response), content_type='text/xml')

    def download_twilio_media(self, media_url):
        """
        Download media from Twilio with proper authentication
        """
        try:
            # Use your Twilio credentials for Basic Auth
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            
            response = requests.get(
                media_url,
                auth=auth,
                timeout=30,
                stream=True
            )
            response.raise_for_status()
            
            # Get the content
            audio_data = response.content
            
            # Verify we actually got audio data
            if len(audio_data) == 0:
                logger.error("Downloaded audio file is empty")
                return None
                
            logger.info(f"✅ Successfully downloaded media: {len(audio_data)} bytes")
            return audio_data
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logger.error("❌ Authentication failed for Twilio media download. Check your Twilio credentials.")
            elif e.response.status_code == 404:
                logger.error("❌ Media file not found. It may have expired.")
            else:
                logger.error(f"❌ HTTP error downloading media: {e}")
            return None
        except requests.exceptions.Timeout:
            logger.error("❌ Timeout downloading media from Twilio")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error downloading media: {e}")
            return None

    def process_whatsapp_audio(self, audio_bytes, input_format):
        """
        Specialized audio processing for WhatsApp OGG files
        Uses FFmpeg directly for better compatibility
        """
        import tempfile
        import os
        import subprocess
        
        try:
            # Create temporary files
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{input_format}') as input_file:
                input_file.write(audio_bytes)
                input_path = input_file.name
            
            output_path = input_path.replace(f'.{input_format}', '.wav')
            
            # Try multiple FFmpeg approaches for WhatsApp OGG files
            ffmpeg_commands = [
                # Best approach for WhatsApp OGG
                [
                    'ffmpeg', '-y', '-i', input_path,
                    '-acodec', 'pcm_s16le', '-ac', '1', '-ar', '16000',
                    '-f', 'wav', output_path
                ],
                # Alternative approach
                [
                    'ffmpeg', '-y', '-i', input_path,
                    '-c:a', 'pcm_s16le', '-ac', '1', '-ar', '16000',
                    output_path
                ],
                # Simple conversion as fallback
                [
                    'ffmpeg', '-y', '-i', input_path,
                    output_path
                ]
            ]
            
            for i, cmd in enumerate(ffmpeg_commands):
                try:
                    logger.info(f"Trying FFmpeg command {i+1} for WhatsApp audio")
                    result = subprocess.run(
                        cmd, 
                        capture_output=True, 
                        text=True, 
                        timeout=30
                    )
                    
                    if result.returncode == 0:
                        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                            with open(output_path, 'rb') as f:
                                wav_data = f.read()
                            
                            logger.info(f"✅ WhatsApp audio processing successful with command {i+1}")
                            
                            # Clean up temporary files
                            try:
                                os.unlink(input_path)
                                os.unlink(output_path)
                            except:
                                pass
                                
                            return wav_data
                        else:
                            logger.warning(f"Command {i+1} succeeded but output file is empty/missing")
                    else:
                        logger.warning(f"FFmpeg command {i+1} failed: {result.stderr}")
                        
                except subprocess.TimeoutExpired:
                    logger.warning(f"FFmpeg command {i+1} timed out")
                except Exception as cmd_error:
                    logger.warning(f"FFmpeg command {i+1} error: {cmd_error}")
            
            # If all FFmpeg approaches fail, fall back to the original normalize_audio function
            logger.info("Falling back to original normalize_audio function")
            from your_audio_module import normalize_audio  # Make sure to import your actual module
            fallback_result = normalize_audio(audio_bytes, input_format)
            
            # Clean up temporary files
            try:
                os.unlink(input_path)
                if os.path.exists(output_path):
                    os.unlink(output_path)
            except:
                pass
                
            return fallback_result
                
        except Exception as e:
            logger.error(f"❌ WhatsApp audio processing completely failed: {str(e)}")
            
            # Clean up any remaining temporary files
            try:
                if 'input_path' in locals() and os.path.exists(input_path):
                    os.unlink(input_path)
                if 'output_path' in locals() and os.path.exists(output_path):
                    os.unlink(output_path)
            except:
                pass
                
            return None