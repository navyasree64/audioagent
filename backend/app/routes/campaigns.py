from fastapi import APIRouter, HTTPException
from app.database import get_db
from app.models import Campaign, BulkCampaignCreate, Lead
from typing import List
import httpx
import os
import asyncio
import uuid

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

@router.get("/", response_model=List[Campaign])
async def get_campaigns():
    db = get_db()
    cursor = db.campaigns.find({})
    campaigns = []
    async for doc in cursor:
        doc["id"] = doc.pop("_id")
        campaigns.append(Campaign(**doc))
    return campaigns

@router.post("/bulk")
async def create_bulk_campaign(bulk_in: BulkCampaignCreate):
    db = get_db()
    
    # 1. Validate Company
    company = await db.companies.find_one({"_id": bulk_in.companyId})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
        
    # 2. Create Campaign
    campaign_id = str(uuid.uuid4())
    campaign_doc = {
        "_id": campaign_id,
        "name": bulk_in.name,
        "companyId": bulk_in.companyId,
        "totalLeads": len(bulk_in.leads),
        "completed": 0,
        "active": 0,
        "successRate": 0.0,
        "eta": "1 day"
    }
    await db.campaigns.insert_one(campaign_doc)
    
    # 3. Create Leads
    if bulk_in.leads:
        leads_to_insert = []
        for lead_data in bulk_in.leads:
            lead_doc = {
                "_id": str(uuid.uuid4()),
                "name": lead_data.name,
                "phone": lead_data.phone,
                "companyId": bulk_in.companyId,
                "status": "PENDING",
                "lastCall": "never",
                "campaign": bulk_in.name,
                "score": 0
            }
            leads_to_insert.append(lead_doc)
            
        await db.leads.insert_many(leads_to_insert)
        
        # Update Company leadCount
        await db.companies.update_one(
            {"_id": bulk_in.companyId},
            {"$inc": {"leadCount": len(bulk_in.leads)}}
        )
        
    return {"status": "success", "campaignId": campaign_id, "leadsImported": len(bulk_in.leads)}

@router.post("/{campaign_id}/launch")
async def launch_campaign(campaign_id: str):
    from app.orchestrator import agent_workflow
    db = get_db()
    
    # 1. Fetch the Campaign
    campaign = await db.campaigns.find_one({"_id": campaign_id})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
        
    # 2. Run LangGraph Workflow
    state = {
        "action": "dispatch",
        "company_id": campaign.get("companyId"),
        "campaign_id": campaign_id,
        "lead_ids": []
    }
    
    try:
        result = await agent_workflow.ainvoke(state)
        launched_count = len(result.get("lead_ids", []))
        return {"status": "success", "launched": launched_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LangGraph execution error: {str(e)}")

