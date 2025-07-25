from fastapi import FastAPI, APIRouter, HTTPException, Query, Body, Depends, status
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from typing import List, Optional
from dotenv import load_dotenv
import httpx
import os

load_dotenv()

# 🔐 Env setup
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_BASE_URL = f"https://{FRESHDESK_DOMAIN}/api/v2"
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "mysecrettoken")

if not FRESHDESK_API_KEY or not FRESHDESK_DOMAIN:
    raise RuntimeError("FRESHDESK_API_KEY or FRESHDESK_DOMAIN missing from .env")

# ✅ FastAPI app
app = FastAPI(title="Freshdesk Ticket Creator with Bearer Auth")

# ✅ Secure routes
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
async def verify_token(token: str = Depends(oauth2_scheme)):
    if token != BEARER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token

# ✅ Dummy token login
@app.post("/token")
async def login():
    return {"access_token": BEARER_TOKEN, "token_type": "bearer"}

# ✅ Ticket model
class TicketCreate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    subject: str
    description: str
    status: int
    priority: int
    type: Optional[str] = None
    tags: Optional[List[str]] = []
    cc_emails: Optional[List[EmailStr]] = []

# ✅ Router with prefix
router = APIRouter(prefix="/v2/arta/proxy", dependencies=[Depends(verify_token)])

# 🎯 All routes moved to `router`

@router.post("/tickets", summary="Create Ticket in Freshdesk")
async def create_ticket(payload: TicketCreate):
    url = f"{FRESHDESK_BASE_URL}/tickets"
    data = payload.dict(exclude_none=True)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            headers={"Content-Type": "application/json"},
            auth=(FRESHDESK_API_KEY, "X"),
            json=data
        )
        if "application/json" not in response.headers.get("content-type", ""):
            return JSONResponse(status_code=500, content={"error": "Expected JSON but got HTML"})
        if response.status_code != 201:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        res_data = response.json()
        return {
            "id": res_data.get("id"),
            "subject": res_data.get("subject"),
            "description": res_data.get("description_text"),
            "status": res_data.get("status"),
            "priority": res_data.get("priority"),
            "requester_id": res_data.get("requester_id")
        }

@router.get("/tickets/filter", summary="Filter tickets by status")
async def filter_tickets_by_status(status: str = Query(..., description="open or closed")):
    status_map = {"open": 2, "closed": 5}
    if status.lower() not in status_map:
        raise HTTPException(status_code=400, detail="Use 'open' or 'closed'.")
    fd_status_code = status_map[status.lower()]
    query = f'"status:{fd_status_code}"'
    url = f"{FRESHDESK_BASE_URL}/search/tickets"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params={"query": query}, auth=(FRESHDESK_API_KEY, "X"))
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()

@router.get("/tickets/search-by-contact", summary="Search tickets by contact number")
async def search_tickets_by_contact_number(number: str = Query(...)):
    results = []
    async with httpx.AsyncClient() as client:
        page = 1
        matched_contacts = []
        while True:
            contact_resp = await client.get(
                f"{FRESHDESK_BASE_URL}/contacts",
                params={"page": page, "per_page": 100},
                auth=(FRESHDESK_API_KEY, "X")
            )
            if contact_resp.status_code != 200:
                raise HTTPException(status_code=contact_resp.status_code, detail=contact_resp.text)
            contacts = contact_resp.json()
            if not contacts:
                break
            for contact in contacts:
                if str(contact.get("phone")) == number or str(contact.get("mobile")) == number:
                    matched_contacts.append(contact)
            page += 1
        if not matched_contacts:
            return {"message": "No contact found with that number."}

        for contact in matched_contacts:
            ticket_resp = await client.get(
                f"{FRESHDESK_BASE_URL}/tickets",
                params={"requester_id": contact["id"]},
                auth=(FRESHDESK_API_KEY, "X")
            )
            if ticket_resp.status_code != 200:
                continue
            tickets = ticket_resp.json()
            for ticket in tickets:
                results.append({
                    "ticket_id": ticket["id"],
                    "subject": ticket.get("subject"),
                    "contact_name": contact.get("name"),
                    "mobile": contact.get("mobile"),
                    "phone": contact.get("phone")
                })
    return results

@router.post("/tickets/{ticket_id}/add-note", summary="Add HTML note")
async def add_note_to_ticket(ticket_id: int, note_body: str = Body(..., embed=True)):
    url = f"{FRESHDESK_BASE_URL}/tickets/{ticket_id}/notes"
    payload = {"body": note_body, "incoming": False}
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json=payload,
            auth=(FRESHDESK_API_KEY, "X")
        )
        if response.status_code != 201:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"message": "Note added", "note": note_body}

@router.delete("/tickets/delete-all", summary="Delete all tickets")
async def delete_all_tickets():
    deleted = []
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{FRESHDESK_BASE_URL}/tickets", auth=(FRESHDESK_API_KEY, "X"))
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Failed to fetch tickets")
        tickets = response.json()
        for ticket in tickets:
            del_resp = await client.delete(
                f"{FRESHDESK_BASE_URL}/tickets/{ticket['id']}",
                auth=(FRESHDESK_API_KEY, "X")
            )
            if del_resp.status_code == 204:
                deleted.append(ticket["id"])
    return {"deleted_tickets": deleted}

# 🔗 Mount router at new prefix
app.include_router(router)
