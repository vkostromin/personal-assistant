from datetime import datetime

from google.cloud import firestore

from src.config import settings
from src.crypto import decrypt
from src.models import Account, GlobalState, SettingsModel

db = firestore.AsyncClient()


async def list_accounts(chat_id: int | None = None) -> list[Account]:
    query = db.collection("accounts").where("enabled", "==", True)
    if chat_id is not None:
        query = query.where("chat_id", "==", chat_id)
    docs = query.stream()
    result = []
    async for doc in docs:
        data = doc.to_dict()
        data["email"] = doc.id
        if data.get("refresh_token"):
            data["refresh_token"] = decrypt(data["refresh_token"], settings.token_encryption_key)
        result.append(Account(**data))
    return result


async def save_account(account: Account) -> None:
    data = account.model_dump(exclude={"email"})
    data["created_at"] = datetime.now()
    await db.collection("accounts").document(account.email).set(data)


async def delete_account(email: str) -> None:
    await db.collection("accounts").document(email).delete()


async def save_draft(draft_data: dict) -> str:
    doc_ref = db.collection("drafts").document()
    draft_data["created_at"] = datetime.now()
    await doc_ref.set(draft_data)
    return doc_ref.id


async def get_pending_drafts(account: str | None = None) -> list[dict]:
    query = db.collection("drafts").where("status", "==", "pending")
    if account:
        query = query.where("account", "==", account)
    docs = query.stream()
    result = []
    async for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        result.append(data)
    return result


async def update_draft(draft_id: str, updates: dict) -> None:
    await db.collection("drafts").document(draft_id).update(updates)


async def delete_draft(draft_id: str) -> None:
    await db.collection("drafts").document(draft_id).delete()


async def save_promise(promise_data: dict) -> str:
    doc_ref = db.collection("promises").document()
    promise_data["created_at"] = datetime.now()
    promise_data["status"] = "open"
    await doc_ref.set(promise_data)
    return doc_ref.id


async def get_open_promises() -> list[dict]:
    query = db.collection("promises").where("status", "==", "open")
    docs = query.stream()
    result = []
    async for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        result.append(data)
    return result


async def close_promise(promise_id: str) -> None:
    await db.collection("promises").document(promise_id).update({"status": "done"})


async def get_state() -> GlobalState:
    doc = await db.collection("state").document("global").get()
    if doc.exists:
        return GlobalState(**doc.to_dict())
    return GlobalState()


async def save_state(state: GlobalState) -> None:
    await db.collection("state").document("global").set(state.model_dump())


async def get_settings() -> SettingsModel:
    doc = await db.collection("settings").document("global").get()
    if doc.exists:
        return SettingsModel(**doc.to_dict())
    return SettingsModel()


async def save_settings(settings: SettingsModel) -> None:
    await db.collection("settings").document("global").set(settings.model_dump())
