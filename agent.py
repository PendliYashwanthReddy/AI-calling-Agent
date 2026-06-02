import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools
from prompts import build_prompt
from tools import AppointmentTools

# override=False → real VPS environment variables ALWAYS win over any local .env.
# On a VPS there is typically no .env file, so config comes purely from the
# process environment. The .env file is only a convenience for local dev.
load_dotenv(".env", override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


# NOTE: Configuration comes EXCLUSIVELY from the process / VPS environment.
# We deliberately do NOT pull any credentials or service settings from the
# database — the VPS environment variables are the single source of truth.


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(tools: list, system_prompt: str,
                   voice: str = None, model: str = None) -> AgentSession:
    """
    Build AgentSession with Gemini Live or pipeline fallback.

    Config defaults come from the environment (single source of truth). Per-call
    `voice`/`model` overrides (from an agent profile in the dispatch metadata) are
    passed in as arguments — we never mutate os.environ, so concurrent jobs on the
    same worker never contaminate each other.

    CRITICAL SILENCE-PREVENTION CONFIG — all 3 required:
    1. SessionResumptionConfig(transparent=True)  — auto-reconnect instead of silence
    2. ContextWindowCompressionConfig(...)         — prevents freeze when context fills
    3. RealtimeInputConfig(... EndSensitivity.END_SENSITIVITY_LOW, silence_duration_ms=2000)
    """
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() == "true"
    model_name = model or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    api_key = os.getenv("GOOGLE_API_KEY", "")

    RealtimeModel = _google_realtime or _google_beta_realtime

    if use_realtime and RealtimeModel is not None:
        from google.genai import types as _gt

        # ── Freeze-prevention configs (keep the session alive, no audio impact) ──
        session_resumption = _gt.SessionResumptionConfig(transparent=True)
        context_window_compression = _gt.ContextWindowCompressionConfig(
            trigger_tokens=25600,
            sliding_window=_gt.SlidingWindow(target_tokens=12800),
        )

        # ── Turn-taking responsiveness (env-tunable; controls how fast the agent
        #    replies after the caller stops speaking). Lower silence + HIGH
        #    sensitivity = snappier. Too low risks the agent interrupting; tune
        #    EOU_SILENCE_MS / EOU_SENSITIVITY without a code change if needed. ──
        _silence_ms = int(os.getenv("EOU_SILENCE_MS", "600"))
        _prefix_ms = int(os.getenv("EOU_PREFIX_MS", "100"))
        _sens_name = os.getenv("EOU_SENSITIVITY", "HIGH").upper()
        _sensitivity = (_gt.EndSensitivity.END_SENSITIVITY_HIGH
                        if _sens_name == "HIGH"
                        else _gt.EndSensitivity.END_SENSITIVITY_LOW)
        realtime_input_config = _gt.RealtimeInputConfig(
            automatic_activity_detection=_gt.AutomaticActivityDetection(
                end_of_speech_sensitivity=_sensitivity,
                silence_duration_ms=_silence_ms,
                prefix_padding_ms=_prefix_ms,
            ),
        )
        logger.info("Turn-taking: silence=%dms, prefix=%dms, eos_sensitivity=%s",
                    _silence_ms, _prefix_ms, _sens_name)

        # Build kwargs defensively — plugin versions differ in what they accept.
        # NOTE: tools are NOT passed to the model/session here — they are attached
        # to the Agent (see OutboundAssistant), which is the canonical and
        # version-stable place for them in livekit-agents 1.x.
        base_kwargs = dict(model=model_name, voice=voice)
        if api_key:
            base_kwargs["api_key"] = api_key

        genai_configs = dict(
            session_resumption=session_resumption,
            context_window_compression=context_window_compression,
            realtime_input_config=realtime_input_config,
        )

        # Try the richest kwarg set first, then progressively drop kwargs that a
        # given plugin version may not support, so we never crash on a TypeError.
        attempts = [
            dict(temperature=0.8, instructions=system_prompt, **genai_configs),
            dict(temperature=0.8, instructions=system_prompt),
            dict(instructions=system_prompt),
            dict(),
        ]
        realtime = None
        for i, extra in enumerate(attempts):
            try:
                realtime = RealtimeModel(**base_kwargs, **extra)
                if "session_resumption" in extra:
                    logger.info("Gemini RealtimeModel built WITH silence-prevention configs")
                else:
                    logger.warning("Gemini RealtimeModel built without some optional kwargs (attempt %d)", i + 1)
                break
            except TypeError as exc:
                logger.warning("RealtimeModel kwargs rejected (%s) — retrying with fewer", exc)
                continue

        if realtime is not None:
            return AgentSession(llm=realtime)
        logger.error("Could not build Gemini RealtimeModel — falling back to pipeline")

    # ── Pipeline fallback (STT → LLM → TTS) ──
    logger.info("Building pipeline-mode AgentSession (fallback)")
    session_kwargs = dict(vad=silero.VAD.load())
    if _deepgram_stt is not None:
        session_kwargs["stt"] = _deepgram_stt()
    if _google_llm is not None:
        session_kwargs["llm"] = _google_llm(model="gemini-2.0-flash", api_key=api_key or None)
    if _google_tts is not None:
        try:
            session_kwargs["tts"] = _google_tts(voice_name=voice)
        except Exception:
            pass
    return AgentSession(**session_kwargs)


# ── Agent ────────────────────────────────────────────────────────────────────

class OutboundAssistant(Agent):
    """Outbound appointment-booking voice agent. Tools live here (1.x canonical)."""

    def __init__(self, instructions: str, tools: list = None) -> None:
        super().__init__(instructions=instructions, tools=tools or [])


# ── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext):
    await _log("info", f"Job received for room: {ctx.room.name}")

    # ── Parse dispatch metadata (job metadata, then room metadata) ───────────
    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override = None

    metadata_raw = ""
    try:
        metadata_raw = ctx.job.metadata or ""
    except Exception:
        metadata_raw = ""
    if not metadata_raw:
        try:
            metadata_raw = ctx.room.metadata or ""
        except Exception:
            metadata_raw = ""

    if metadata_raw:
        try:
            data = json.loads(metadata_raw)
            phone_number   = data.get("phone_number")
            lead_name      = data.get("lead_name") or lead_name
            business_name  = data.get("business_name") or business_name
            service_type   = data.get("service_type") or service_type
            custom_prompt  = data.get("system_prompt")
            voice_override = data.get("voice_override")
            model_override = data.get("model_override")
            tools_override = data.get("tools_override")
        except Exception as exc:
            await _log("warning", f"Could not parse dispatch metadata: {exc}")

    # ── Resolve per-call config (env defaults, optionally overridden by profile) ──
    # We do NOT mutate os.environ — the environment stays the single source of
    # truth and concurrent jobs on this worker never contaminate each other.
    effective_voice = voice_override or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    effective_model = model_override or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    if voice_override:
        await _log("info", f"Voice override for this call: {voice_override}")
    if model_override:
        await _log("info", f"Model override for this call: {model_override}")

    # ── Resolve enabled tools (profile override → global setting → all) ──────
    enabled_tools: list = []
    if tools_override:
        try:
            parsed = json.loads(tools_override) if isinstance(tools_override, str) else tools_override
            if isinstance(parsed, list):
                enabled_tools = parsed
        except Exception:
            enabled_tools = []
    if not enabled_tools:
        try:
            enabled_tools = await get_enabled_tools()
        except Exception:
            enabled_tools = []

    # ── Build the system prompt ──────────────────────────────────────────────
    system_prompt = build_prompt(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        custom_prompt=custom_prompt,
    )

    # ── Tool context ─────────────────────────────────────────────────────────
    tool_ctx = AppointmentTools(ctx, phone_number=phone_number, lead_name=lead_name)

    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Dial — MUST come before session.start() ──────────────────────────────
    if phone_number:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot place outbound call")
            ctx.shutdown()
            return
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            ctx.shutdown()
            return
        await _log("info", f"Call ANSWERED — {phone_number} picked up, starting AI session now")

    # ── Build and start Gemini Live ──────────────────────────────────────────
    await _log("info", f"Building AI session — model={effective_model}, voice={effective_voice}")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    session = _build_session(
        tools=active_tools, system_prompt=system_prompt,
        voice=effective_voice, model=effective_model,
    )

    # Tools are attached to the Agent (canonical place in livekit-agents 1.x).
    agent = OutboundAssistant(instructions=system_prompt, tools=active_tools)

    # NEVER use close_on_disconnect=True with SIP — drops on any audio blip.
    # room_input_options is the stable API across 1.x; fall back gracefully if a
    # given version rejects noise_cancellation.
    try:
        await session.start(
            room=ctx.room,
            agent=agent,
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        )
    except TypeError as exc:
        await _log("warning", f"room_input_options rejected ({exc}) — starting without noise cancellation")
        await session.start(room=ctx.room, agent=agent)
    await _log("info", "Agent session started — AI ready, generating greeting")

    # ── Optional S3 recording ────────────────────────────────────────────────
    if phone_number:
        _aws_key    = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
        _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
        _s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
        _s3_region  = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
        if _aws_key and _aws_secret and _aws_bucket:
            try:
                _recording_path = f"recordings/{ctx.room.name}.ogg"
                _egress_req = api.RoomCompositeEgressRequest(
                    room_name=ctx.room.name, audio_only=True,
                    file_outputs=[api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG, filepath=_recording_path,
                        s3=api.S3Upload(access_key=_aws_key, secret=_aws_secret,
                                        bucket=_aws_bucket, region=_s3_region, endpoint=_s3_endpoint),
                    )],
                )
                _egress = await ctx.api.egress.start_room_composite_egress(_egress_req)
                _s3_ep = _s3_endpoint.rstrip("/")
                tool_ctx.recording_url = (f"{_s3_ep}/{_aws_bucket}/{_recording_path}"
                                           if _s3_ep else f"s3://{_aws_bucket}/{_recording_path}")
                await _log("info", f"Recording started: egress={_egress.egress_id}")
            except Exception as _exc:
                await _log("warning", f"Recording start failed (non-fatal): {_exc}")

    # ── Greeting ─────────────────────────────────────────────────────────────
    # gemini-3.1 and gemini-2.5 native-audio speak autonomously from system prompt.
    # generate_reply() is blocked by the plugin for these models — skip it entirely.
    _active_model = effective_model
    if "3.1" in _active_model or "2.5" in _active_model:
        await _log("info", "Gemini native-audio: model will greet autonomously from system prompt")
    else:
        greeting = (
            f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}."
            if phone_number else "Greet the caller warmly."
        )
        try:
            await session.generate_reply(instructions=greeting)
        except Exception as _gr_exc:
            await _log("warning", f"generate_reply failed: {_gr_exc}")

    # ── Keep session alive until SIP participant actually leaves ─────────────
    # Without this block, the entrypoint returns and the process spins down.
    # We watch participant_disconnected for the specific SIP identity.
    if phone_number:
        _sip_identity = f"sip_{phone_number}"
        _disconnect_event = asyncio.Event()

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == _sip_identity:
                _disconnect_event.set()
        def _on_disconnected():
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_disconnected)

        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")

        await _log("info", f"SIP participant disconnected — ending session for {phone_number}")
        await session.aclose()
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
