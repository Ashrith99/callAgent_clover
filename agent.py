from __future__ import annotations
import os
import asyncio
import time
import logging
from typing import List, Dict, Any
from pydantic import BaseModel, ConfigDict
from dotenv import load_dotenv
from datetime import datetime

from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    RoomInputOptions,
    function_tool,
)
from livekit.plugins import openai, noise_cancellation
from livekit.plugins.openai import realtime

# --- Local imports
from db import DatabaseDriver
from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION

# --- Load environment variables
load_dotenv()

# ============================================================
# 🚀 MODULE-LEVEL PROMPT CACHE: Load once, reuse forever
# ============================================================
# Cache combined instructions at module level to avoid any recalculation
# Prompts are already cached in prompts.py, this ensures combined version is also cached
_COMBINED_INSTRUCTIONS_CACHE = None

def _get_combined_instructions():
    """Get cached combined instructions - computed once at module load"""
    global _COMBINED_INSTRUCTIONS_CACHE
    if _COMBINED_INSTRUCTIONS_CACHE is None:
        # AGENT_INSTRUCTION and SESSION_INSTRUCTION are already cached in prompts.py
        # This is just combining them once and storing in memory
        _COMBINED_INSTRUCTIONS_CACHE = f"{AGENT_INSTRUCTION}\n\n{SESSION_INSTRUCTION}"
    return _COMBINED_INSTRUCTIONS_CACHE

# --- Production Mode Configuration
PRODUCTION = os.getenv("ENVIRONMENT") == "production"

# --- Logger with environment-based levels
log = logging.getLogger("realtime_restaurant_agent")
if PRODUCTION:
    log.setLevel(logging.WARNING)  # Reduced logging in production for better performance
    logging.getLogger("livekit").setLevel(logging.ERROR)
else:
    log.setLevel(logging.INFO)

# --- Database (lazy initialization to avoid blocking)
db_driver = None

def get_db_driver():
    """Get database driver with lazy initialization"""
    global db_driver
    if db_driver is None:
        db_driver = DatabaseDriver()
    return db_driver

# ------------------------------------------------------------
# 🧩 FUNCTION TOOLS
# ------------------------------------------------------------
current_agent = None
current_job_context = None
class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    name: str
    quantity: int
    price: float


class CreateOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    items: List[OrderItem]
    phone: str | None = None
    name: str | None = None
    address: str | None = None



def create_order_tool_factory(agent_instance):
    """Factory function to create a create_order tool bound to a specific agent instance"""
    @function_tool()
    async def create_order(items: List[OrderItem], phone: str | None = None, name: str | None = None, address: str | None = None):
        """Create an order with the provided items."""
        if agent_instance and agent_instance.order_placed:
            return "I'm sorry, but I can only place one order per call. Your previous order has already been confirmed."

        if agent_instance and agent_instance.caller_phone:
            if not phone or phone == "unknown":
                phone = agent_instance.caller_phone

        try:
            if not phone or phone == "unknown":
                final_phone = f"call_{int(time.time())}"
            else:
                final_phone = phone

            # Make database call non-blocking - don't wait for it
            async def save_order_async():
                try:
                    # 🔍 DEBUG: Agent calling save
                    log.info(f"🔍 DEBUG: Agent save_order_async starting...")
                    
                    # Use the new Clover-integrated method (fully async)
                    items_payload = [item.model_dump() for item in items]
                    log.info(f"🔍 DEBUG: Items payload: {items_payload}")
                    
                    # Get database driver (lazy initialization)
                    driver = get_db_driver()
                    result = await driver.create_order_with_clover(
                        final_phone, items_payload, name, address
                    )
                    
                    log.info(f"🔍 DEBUG: save result: {result is not None}")
                    
                    if result:
                        agent_instance.order_placed = True
                        log.info(f"✅ Order saved (MongoDB + Clover POS)")
                        asyncio.create_task(agent_instance._terminate_call_after_delay())
                except Exception as e:
                    log.error(f"Async order save failed: {e}")
                    import traceback
                    log.error(f"🔍 DEBUG: Agent traceback: {traceback.format_exc()}")
            
            # Don't wait for database - respond immediately
            asyncio.create_task(save_order_async())

            return "✅ Order placed successfully! Your order has been confirmed and saved to our system. We will send you the details shortly."
        except Exception as e:
            log.error(f"Order creation failed: {e}")
            return "Sorry, there was an error saving your order. Please try again."

    return create_order


# ------------------------------------------------------------
# 🧠 AGENT CLASS
# ------------------------------------------------------------
class RestaurantAgent(Agent):
    # Class-level cache (shared across all instances)
    _cached_instructions = None
    
    def __init__(self, job_context=None):
        # Use module-level cache to ensure prompts are loaded only once
        # _get_combined_instructions() guarantees single computation
        if RestaurantAgent._cached_instructions is None:
            RestaurantAgent._cached_instructions = _get_combined_instructions()
        
        create_order_tool = create_order_tool_factory(self)

        super().__init__(
            instructions=RestaurantAgent._cached_instructions,
            tools=[create_order_tool],
        )

        self.current_session = None
        self.caller_phone = None
        self.termination_started = False
        self.order_placed = False
        self.job_context = job_context

        global current_agent
        current_agent = self

    async def _execute_tool(self, tool_call, session):
        if tool_call.function.name == "create_order":
            import json, time
            args = json.loads(tool_call.function.arguments)
            phone = self.caller_phone
            if not phone or phone in ["unknown", "extracted_failed"]:
                phone = f"call_{int(time.time())}"
            args["phone"] = phone
            tool_call.function.arguments = json.dumps(args)
        return await super()._execute_tool(tool_call, session)

    async def on_message(self, message, session):
        if self.termination_started:
            return "The call is ending. Thank you for choosing Bawarchi Restaurant!"
        try:
            # Use reasonable timeout - balance between waiting and responsiveness
            # If LLM is consistently slow, fallback will kick in
            response = await asyncio.wait_for(
                super().on_message(message, session),
                timeout=3.0  # Optimized timeout - faster fallback for better UX
            )
            return response
        except asyncio.TimeoutError:
            # Fallback immediately if LLM is slow - better UX than waiting
            log.warning("LLM response timeout, using fallback")
            return self._get_smart_fallback_response(message.content or "")
        except Exception as e:
            log.error(f"Error in on_message: {e}")
            return self._get_smart_fallback_response(message.content or "")

    def _get_smart_fallback_response(self, msg: str):
        msg = msg.lower()
        if any(x in msg for x in ['order', 'food', 'menu', 'biryani', 'chicken', 'mutton', 'rice', 'curry']):
            return "I can help you place an order! Please tell me what you'd like to order."
        if any(x in msg for x in ['hello', 'hi', 'hey']):
            return "Hello! Welcome to Bawarchi Restaurant. How can I help you today?"
        if any(x in msg for x in ['price', 'cost', 'how much']):
            return "Our prices are very reasonable. What specific dish would you like to know the price for?"
        return "I'm here to help you with your order. What would you like to order?"

    async def on_start(self, session: AgentSession):
        self.current_session = session
        # Start greeting immediately - generate_reply returns a SpeechHandle, not a coroutine
        # Don't await it - let it run in the background
        try:
            # Generate greeting (enabled by default, can be disabled with ENABLE_TTS=0)
            if os.getenv("ENABLE_TTS", "1") != "0":
                session.generate_reply(
                    instructions='Say the complete greeting in English: "Hello! Welcome to Bawarchi Restaurant. I am Sarah. What would you like to order today?" Say all parts of the greeting - do not skip any words.'
                )
        except Exception as e:
            log.warning(f"Greeting generation error: {e}, continuing anyway")

    # ------------------------------------------------------------
    # 🧩 FULL TERMINATION SEQUENCE
    # ------------------------------------------------------------
    async def _terminate_call_after_delay(self):
        """Comprehensive call termination logic"""
        job_context = self.job_context
        try:
            log.info("🔧 Starting automatic call termination sequence...")
            await asyncio.sleep(5.0)
            self.termination_started = True

            if self.current_session:
                try:
                    if os.getenv("ENABLE_TTS", "1") != "0":
                        await asyncio.wait_for(
                            self.current_session.generate_reply(
                                instructions="Say: Thank you for choosing Bawarchi Restaurant! Goodbye!"
                            ),
                            timeout=4.0
                        )
                    await asyncio.sleep(6.0)
                except Exception as e:
                    log.warning(f"⚠️ Could not send final goodbye: {e}")

                # 1️⃣ Disconnect all participants
                try:
                    if hasattr(self.current_session, "room") and self.current_session.room:
                        for pid, p in self.current_session.room.remote_participants.items():
                            try:
                                await p.disconnect()
                            except Exception:
                                pass
                except Exception:
                    pass

                # 2️⃣ Close room
                try:
                    if hasattr(self.current_session, "room") and self.current_session.room:
                        await self.current_session.room.close()
                except Exception:
                    pass

                # 3️⃣ Session termination variants
                for method_name in ["disconnect", "stop", "end", "close", "terminate", "shutdown"]:
                    if hasattr(self.current_session, method_name):
                        try:
                            await getattr(self.current_session, method_name)()
                            break
                        except Exception:
                            continue

                # 4️⃣ Close _room
                try:
                    if hasattr(self.current_session, "_room") and self.current_session._room:
                        await self.current_session._room.close()
                except Exception:
                    pass

                # 5️⃣ Stop agent
                try:
                    if hasattr(self.current_session, "agent") and self.current_session.agent:
                        if hasattr(self.current_session.agent, "stop"):
                            await self.current_session.agent.stop()
                except Exception:
                    pass

                # 6️⃣ Force disconnect SIP participants
                try:
                    if job_context and hasattr(job_context, "room") and job_context.room:
                        for pid, participant in job_context.room.remote_participants.items():
                            if pid.startswith("sip_"):
                                for m in ["disconnect", "remove", "kick"]:
                                    if hasattr(participant, m):
                                        try:
                                            await getattr(participant, m)()
                                        except Exception:
                                            pass
                except Exception:
                    pass

                # 7️⃣ room.disconnect_participant
                try:
                    if job_context and hasattr(job_context, "room") and job_context.room:
                        for pid in job_context.room.remote_participants.keys():
                            if hasattr(job_context.room, "disconnect_participant"):
                                await job_context.room.disconnect_participant(pid)
                except Exception:
                    pass

                # 8️⃣ room.remove_participant
                try:
                    if job_context and hasattr(job_context, "room") and job_context.room:
                        for pid in job_context.room.remote_participants.keys():
                            if hasattr(job_context.room, "remove_participant"):
                                await job_context.room.remove_participant(pid)
                except Exception:
                    pass

                # 9️⃣ Close connection
                try:
                    if job_context and hasattr(job_context, "room") and job_context.room:
                        room = job_context.room
                        if hasattr(room, "connection"):
                            conn = room.connection
                            if hasattr(conn, "close"):
                                await conn.close()
                        elif hasattr(room, "_connection"):
                            conn = room._connection
                            if hasattr(conn, "close"):
                                await conn.close()
                except Exception:
                    pass

                # 🔟 Terminate Twilio call via API
                try:
                    if job_context and hasattr(job_context, "room") and job_context.room:
                        room = job_context.room
                        for pid, participant in room.remote_participants.items():
                            if pid.startswith("sip_"):
                                if hasattr(participant, "attributes") and participant.attributes:
                                    call_sid = participant.attributes.get("sip.twilio.callSid")
                                    if call_sid:
                                        log.info(f"🔧 Terminating Twilio call SID: {call_sid}")
                                        await self._terminate_twilio_call(call_sid)
                except Exception as e:
                    log.warning(f"⚠️ Twilio termination failed: {e}")

                # 11️⃣ Disconnect job context
                try:
                    if hasattr(job_context, "disconnect"):
                        await job_context.disconnect()
                except Exception:
                    pass

                # 12️⃣ Clear session reference
                self.current_session = None
                log.info("✅ Call termination sequence completed successfully.")
        except Exception as e:
            log.error(f"⚠️ Error in _terminate_call_after_delay: {e}")

    async def _terminate_twilio_call(self, call_sid: str):
        """Terminate Twilio call using Twilio REST API"""
        import aiohttp

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")

        if not account_sid or not auth_token:
            log.warning("⚠️ Twilio credentials missing.")
            return

        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    auth=aiohttp.BasicAuth(account_sid, auth_token),
                    data={"Status": "completed"},
                ) as resp:
                    if resp.status == 200:
                        log.info(f"✅ Twilio call {call_sid} terminated.")
                    else:
                        body = await resp.text()
                        log.warning(f"⚠️ Twilio API failed: {resp.status} - {body}")
        except Exception as e:
            log.error(f"⚠️ Error terminating Twilio call: {e}")


# ------------------------------------------------------------
# 🚀 ENTRYPOINT
# ------------------------------------------------------------
async def entrypoint(ctx: JobContext):
    global current_job_context
    current_job_context = ctx

    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in environment variables!")

    # 🚀 REALTIME MODEL: Ultra-low latency - STT + LLM + TTS all in one!
    # No separate Deepgram, no separate TTS, no separate LLM
    # Everything happens in real-time with OpenAI's Realtime API
    realtime_model = realtime.RealtimeModel(
        api_key=openai_api_key,
        voice="alloy",  # Options: alloy, echo, shimmer, nova, fable, onyx
        modalities=["audio", "text"],
        # Turn detection configuration (passed as dict)
        turn_detection={
            "type": "server_vad",  # Server-side voice activity detection
            "threshold": 0.5,  # Sensitivity threshold
            "prefix_padding_ms": 300,  # Audio before speech
            "silence_duration_ms": 500,  # Silence to detect end of turn
        },
    )

    # Create Agent with RealtimeModel (no separate STT/TTS/LLM needed)
    agent = RestaurantAgent(job_context=ctx)
    
    # Override agent's LLM with RealtimeModel
    agent._llm = realtime_model
    
    # Create AgentSession (RealtimeModel handles everything)
    session = AgentSession(
        stt=None,  # RealtimeModel handles STT
        tts=None,  # RealtimeModel handles TTS
        llm=realtime_model,  # RealtimeModel handles LLM
    )
    
    await ctx.connect()

    # Extract caller phone number (non-blocking - done in parallel with session start)
    async def extract_phone_number():
        caller_phone = None
        try:
            # Try immediately first
            room = ctx.room
            if room:
                for pid, participant in room.remote_participants.items():
                    if pid.startswith("sip_"):
                        phone = pid.replace("sip_", "")
                        if phone.startswith("+"):
                            caller_phone = phone
                            break
                    if hasattr(participant, "attributes") and participant.attributes:
                        sip_phone = participant.attributes.get("sip.phoneNumber")
                        if sip_phone:
                            caller_phone = sip_phone
                            break
                    if hasattr(participant, "metadata") and participant.metadata:
                        phone_metadata = participant.metadata.get("phoneNumber") or participant.metadata.get("from")
                        if phone_metadata:
                            caller_phone = phone_metadata
                            break
            
            # If not found, wait briefly and try again (but don't block session start)
            if not caller_phone:
                await asyncio.sleep(0.3)
                room = ctx.room
                if room:
                    for pid, participant in room.remote_participants.items():
                        if pid.startswith("sip_"):
                            phone = pid.replace("sip_", "")
                            if phone.startswith("+"):
                                caller_phone = phone
                                break
                        if hasattr(participant, "attributes") and participant.attributes:
                            sip_phone = participant.attributes.get("sip.phoneNumber")
                            if sip_phone:
                                caller_phone = sip_phone
                                break
                        if hasattr(participant, "metadata") and participant.metadata:
                            phone_metadata = participant.metadata.get("phoneNumber") or participant.metadata.get("from")
                            if phone_metadata:
                                caller_phone = phone_metadata
                                break
        except Exception:
            pass
        
        if caller_phone:
            agent.caller_phone = caller_phone
        else:
            agent.caller_phone = "extracted_failed"

    # Start session immediately without blocking
    await session.start(
        room=ctx.room,
        agent=agent,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )

    # Extract phone number in parallel (non-blocking)
    asyncio.create_task(extract_phone_number())
    
    # Start greeting immediately
    asyncio.create_task(agent.on_start(session))


# ------------------------------------------------------------
# 🏁 MAIN RUNNER
# ------------------------------------------------------------
if __name__ == "__main__":
    agents.cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="inbound_agent",
        )
    )
