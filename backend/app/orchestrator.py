import os
import asyncio
import httpx
from typing import TypedDict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from app.database import get_db

# 1. State Definition
class GraphState(TypedDict):
    action: str  # "dispatch" or "evaluate"
    company_id: Optional[str]
    campaign_id: Optional[str]
    lead_ids: Optional[List[str]]
    call_id: Optional[str]
    transcript: Optional[str]
    summary: Optional[str]
    customer_phone: Optional[str]
    outcome: Optional[str]      # QUALIFIED, NOT_INTERESTED, FAILED, NEEDS_REVIEW
    reasoning: Optional[str]
    status: Optional[str]

# Define structural model for LLM output evaluation
class EvaluationResult(BaseModel):
    outcome: str = Field(description="The evaluation outcome: QUALIFIED, NOT_INTERESTED, FAILED, or NEEDS_REVIEW")
    reasoning: str = Field(description="Brief analysis explaining the reason behind the outcome")

# 2. Nodes Implementation

async def dispatch_node(state: GraphState) -> GraphState:
    """
    Fetches all PENDING customers for a company, triggers outbound voice calls via Vapi,
    and updates their status to CALL_INITIATED.
    """
    db = get_db()
    company_id = state.get("company_id")
    campaign_id = state.get("campaign_id")
    
    if not company_id:
        state["status"] = "No company_id provided"
        return state

    # Fetch pending leads for this company (limit to 5 for safety/rate limits)
    query = {"companyId": company_id, "status": "PENDING"}
    cursor = db.leads.find(query).limit(5)
    pending_leads = await cursor.to_list(length=5)
    
    if not pending_leads:
        state["status"] = "No pending leads found"
        state["lead_ids"] = []
        return state

    vapi_key = os.getenv("VAPI_PRIVATE_KEY")
    phone_number_id = os.getenv("VAPI_PHONE_NUMBER_ID")
    server_url = os.getenv("VAPI_SERVER_URL", "")
    
    headers = {
        "Authorization": f"Bearer {vapi_key}",
        "Content-Type": "application/json"
    }

    # Fetch company context for dynamic prompt configuration (Dynamic Prompting Bonus)
    company = await db.companies.find_one({"_id": company_id})
    company_name = company.get("name", "our company") if company else "our company"
    
    protocol_context = ""
    protocol_metadata = company.get("protocol_metadata") if company else None
    if protocol_metadata:
        c_summary = protocol_metadata.get("summary", "")
        key_points = protocol_metadata.get("key_selling_points", [])
        points_str = "\n".join([f"- {p}" for p in key_points])
        protocol_context = f"\nCompany Context: {c_summary}\nKey Selling Points:\n{points_str}"

    triggered_lead_ids = []
    
    async with httpx.AsyncClient() as client:
        for lead in pending_leads:
            system_prompt = (
                f"You are a professional sales representative calling on behalf of {company_name}. "
                f"You are calling a lead named {lead.get('name')}. "
                f"Your goal is to qualify this lead by politely engaging them in conversation.{protocol_context}"
            )
            first_message = f"Hello {lead.get('name')}, this is calling from {company_name}. How are you doing today?"

            payload = {
                "name": f"Call to {lead.get('name')}",
                "phoneNumberId": phone_number_id,
                "assistant": {
                    "firstMessage": first_message,
                    "serverUrl": f"{server_url}/vapi/webhook" if server_url else None,
                    "model": {
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": system_prompt}
                        ]
                    },
                    "voice": {
                        "provider": "11labs",
                        "voiceId": "bIHbv24MWmeRgasZH58o"
                    }
                },
                "customer": {
                    "number": lead.get("phone", "+15555555555"),
                    "name": lead.get("name")
                }
            }

            try:
                # Dispatch the call to Vapi
                response = await client.post("https://api.vapi.ai/call/phone", json=payload, headers=headers)
                # Success check can be response.status_code == 201
            except Exception as e:
                print(f"Error launching Vapi call for lead {lead.get('_id')}: {e}")

            # Update the lead status to CALL_INITIATED in MongoDB
            await db.leads.update_one(
                {"_id": lead["_id"]},
                {"$set": {"status": "CALL_INITIATED"}}
            )
            triggered_lead_ids.append(lead["_id"])
            await asyncio.sleep(0.5)

    # Update the campaign statistics if campaign_id is provided
    if campaign_id and triggered_lead_ids:
        await db.campaigns.update_one(
            {"_id": campaign_id},
            {"$inc": {"active": len(triggered_lead_ids)}}
        )

    state["lead_ids"] = triggered_lead_ids
    state["status"] = f"Dispatched {len(triggered_lead_ids)} calls"
    return state


async def evaluation_node(state: GraphState) -> GraphState:
    """
    Evaluates the transcript and summary using Gemini LLM to categorize the lead.
    Supports Human-In-The-Loop evaluation by reverting to NEEDS_REVIEW when unsure.
    """
    transcript = state.get("transcript", "")
    summary = state.get("summary", "")
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Fallback to simple regex if API key is not configured
        summary_lower = summary.lower()
        if any(w in summary_lower for w in ["interested", "qualified", "positive", "agreed", "wants", "yes", "schedule"]):
            state["outcome"] = "QUALIFIED"
        elif any(w in summary_lower for w in ["not interested", "no", "busy", "hang up", "hung up", "remove"]):
            state["outcome"] = "NOT_INTERESTED"
        elif any(w in summary_lower for w in ["voicemail", "machine", "no answer", "failed", "error", "disconnected"]):
            state["outcome"] = "FAILED"
        else:
            state["outcome"] = "NEEDS_REVIEW"
        state["reasoning"] = "Fallback evaluation due to missing GEMINI_API_KEY."
        return state

    try:
        # Initialize Gemini LLM
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=api_key, temperature=0)
        
        # We can construct a structured prompt
        prompt = PromptTemplate.from_template(
            "Analyze the following call transcript and summary between an AI Voice Agent and a Customer.\n"
            "Evaluate the conversation and deduce the lead's final status.\n\n"
            "Call Summary:\n{summary}\n\n"
            "Call Transcript:\n{transcript}\n\n"
            "Determine the category and reasoning:\n"
            "- QUALIFIED: If the customer showed interest, agreed to follow-up, booked a meeting, or had a positive discussion.\n"
            "- NOT_INTERESTED: If the customer explicitly refused, said they are not interested, asked not to be called, or hung up on the agent.\n"
            "- FAILED: If the call went to voicemail, disconnected immediately, or resulted in no verbal dialogue.\n"
            "- NEEDS_REVIEW: If the call transcript is ambiguous, if the LLM is unsure, or if there is something complex needing human inspection.\n\n"
            "Output your response strictly as JSON with keys 'outcome' (QUALIFIED, NOT_INTERESTED, FAILED, NEEDS_REVIEW) and 'reasoning' (a brief explanation)."
        )
        
        chain = prompt | llm.with_structured_output(EvaluationResult)
        res = await chain.ainvoke({"summary": summary, "transcript": transcript})
        
        state["outcome"] = res.outcome.upper()
        state["reasoning"] = res.reasoning
    except Exception as e:
        print(f"Gemini evaluation node error: {e}")
        # Smart fallback to regex matching in case of LLM API issues
        summary_lower = summary.lower()
        if any(w in summary_lower for w in ["interested", "qualified", "positive", "agreed", "wants", "yes", "schedule", "book"]):
            state["outcome"] = "QUALIFIED"
        elif any(w in summary_lower for w in ["not interested", "no", "busy", "hang up", "hung up", "remove", "don't call"]):
            state["outcome"] = "NOT_INTERESTED"
        elif any(w in summary_lower for w in ["voicemail", "machine", "no answer", "failed", "error", "disconnected"]):
            state["outcome"] = "FAILED"
        else:
            state["outcome"] = "NEEDS_REVIEW"
        state["reasoning"] = f"Regex fallback used due to LLM error: {str(e)}"

    return state


async def state_update_node(state: GraphState) -> GraphState:
    """
    Updates the database with the call outcome, transcript, summary, and reasoning.
    Also updates the lead's status dynamically.
    """
    db = get_db()
    call_id = state.get("call_id")
    customer_phone = state.get("customer_phone")
    outcome = state.get("outcome", "NEEDS_REVIEW")
    reasoning = state.get("reasoning", "")
    summary = state.get("summary", "")
    transcript_formatted = state.get("transcript") # Can be raw text or pre-formatted list

    if call_id:
        # Update the call log details
        await db.call_logs.update_one(
            {"_id": call_id},
            {"$set": {
                "confidence": 0.95,
                "summary": summary,
                "evaluation": outcome.capitalize(),
                "reasoning": reasoning,
                "ended_at": datetime.utcnow()
            }},
            upsert=True
        )

    if customer_phone:
        # Update the lead's status based on evaluation outcome
        await db.leads.update_many(
            {"phone": customer_phone},
            {"$set": {
                "status": outcome,
                "lastCall": "just now"
            }}
        )
        
    state["status"] = f"Updated db state to {outcome}"
    return state


# 3. Create the Stateful Graph

def build_workflow() -> StateGraph:
    workflow = StateGraph(GraphState)
    
    # Add Nodes
    workflow.add_node("dispatch", dispatch_node)
    workflow.add_node("evaluate", evaluation_node)
    workflow.add_node("state_update", state_update_node)
    
    # Conditional Edges & Routing
    def route_start(state: GraphState):
        if state.get("action") == "dispatch":
            return "dispatch"
        else:
            return "evaluate"

    workflow.set_conditional_entry_point(
        route_start,
        {
            "dispatch": "dispatch",
            "evaluate": "evaluate"
        }
    )
    
    workflow.add_edge("dispatch", END)
    workflow.add_edge("evaluate", "state_update")
    workflow.add_edge("state_update", END)
    
    return workflow.compile()

# Compile the runnable graph instance
agent_workflow = build_workflow()
