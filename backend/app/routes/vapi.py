from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse
from app.database import get_db
from datetime import datetime
import json

router = APIRouter(prefix="/vapi", tags=["vapi"])

@router.post("/webhook")
async def vapi_webhook(request: Request):
    db = get_db()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    # Vapi sends different message types, e.g., 'call-update', 'end-of-call-report'
    message_type = body.get("message", {}).get("type")
    call = body.get("message", {}).get("call", {})
    call_id = call.get("id")
    
    if not call_id:
        return {"status": "ignored"}
        
    if message_type == "end-of-call-report":
        transcript = body.get("message", {}).get("transcript", "")
        summary = body.get("message", {}).get("summary", "")
        
        # Parse transcript into the expected array structure
        artifact = body.get("message", {}).get("artifact", {})
        messages = artifact.get("messages", [])
        transcript_formatted = []
        
        if messages:
            for msg in messages:
                role = msg.get("role")
                text = msg.get("message")
                if role and text:
                    speaker = "ai" if role in ["assistant", "bot", "system"] else "customer"
                    transcript_formatted.append({"speaker": speaker, "message": text})
        elif transcript:
            import re
            parts = re.split(r'(AI:|User:)', transcript)
            current_speaker = "system"
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if part == "AI:":
                    current_speaker = "ai"
                elif part == "User:":
                    current_speaker = "customer"
                else:
                    transcript_formatted.append({
                        "speaker": current_speaker,
                        "message": part
                    })
        
        # update call in db initially
        customer_name = call.get("customer", {}).get("name", "Vapi Outbound Call")
        customer_phone = call.get("customer", {}).get("number")
        
        await db.call_logs.update_one(
            {"_id": call_id},
            {"$set": {
                "customer": customer_name, 
                "duration": "00:00",
                "transcript": transcript_formatted,
                "ended_at": datetime.utcnow()
            }},
            upsert=True
        )
        
        # Invoke LangGraph evaluation workflow
        from app.orchestrator import agent_workflow
        state = {
            "action": "evaluate",
            "call_id": call_id,
            "transcript": transcript,
            "summary": summary,
            "customer_phone": customer_phone,
            "outcome": None,
            "reasoning": None
        }
        try:
            await agent_workflow.ainvoke(state)
        except Exception as e:
            print(f"Error during LangGraph evaluation in webhook: {e}")
    elif message_type == "status-update":
        status = call.get("status")
        await db.call_logs.update_one(
            {"_id": call_id},
            {"$set": {
                "status": status
            }},
            upsert=True
        )

    return {"status": "success"}

@router.get("/transcript/{call_id}")
async def get_transcript(call_id: str):
    db = get_db()
    doc = await db.calls.find_one({"call_id": call_id})
    if not doc or not doc.get("transcript"):
        raise HTTPException(status_code=404, detail="Transcript not found")
        
    return PlainTextResponse(doc["transcript"])
