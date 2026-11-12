import pytest
from unittest.mock import AsyncMock
from httpx import AsyncClient
from database.database import db
from telegram_bot.bot_app import bot

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mock_bot_notifications(monkeypatch):
    monkeypatch.setattr(bot, "send_message", AsyncMock())
    monkeypatch.setattr(bot, "send_photo", AsyncMock())


async def test_subscription_crud(client: AsyncClient):
    # 1. Create a subscription via admin
    create_payload = {
        "name": "Абонемент 10 часов",
        "hours": 10,
        "price": 500000,
        "description": "10 часов игры",
        "days_valid": 30,
        "is_active": 1
    }
    resp = await client.post("/subscriptions/admin/create", json=create_payload)
    assert resp.status_code == 201
    data = resp.json()
    sub_id = data["id"]
    assert data["status"] == "created"

    # 2. Get available subscriptions for clients
    resp = await client.get("/subscriptions")
    assert resp.status_code == 200
    subs = resp.json()
    assert any(s["id"] == sub_id and s["hours"] == 10 for s in subs)

    # 3. Update subscription
    update_payload = {
        "price": 550000,
        "description": "Обновленное описание"
    }
    resp = await client.put(f"/subscriptions/admin/{sub_id}", json=update_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"

    # Verify update
    sub = await db.get_subscription_by_id(sub_id)
    assert sub is not None
    assert sub["price"] == 550000
    assert sub["description"] == "Обновленное описание"


async def test_subscription_flow_and_booking(client: AsyncClient):
    tg_id = 999888777

    # Ensure user exists in users table
    await db.execute(
        "INSERT OR IGNORE INTO users (id, name, username, number, language, OfferOk) VALUES (?, ?, ?, ?, ?, ?)",
        (tg_id, "Test Player", "test_player", "+998901112233", "ru", 1),
        commit=True
    )

    # 1. Create a subscription plan
    sub_id = await db.create_subscription(
        name="Тестовый абонемент 5 часов",
        hours=5,
        price=300000,
        description="5 часов",
        days_valid=30,
        is_active=1
    )

    # 2. Check profile before subscription
    prof_resp = await client.get(f"/profile/{tg_id}")
    assert prof_resp.status_code == 200
    prof_data = prof_resp.json()
    assert "subscription" in prof_data
    assert prof_data["subscription"]["has_subscription"] is False
    assert prof_data["subscription"]["available_hours"] == 0.0

    # 3. Buy subscription: create pending purchase
    purchase_id = "test_purchase_123"
    await db.create_pending_subscription_purchase(
        purchase_id=purchase_id,
        telegram_id=tg_id,
        subscription_id=sub_id,
        price=300000
    )

    # Check pending purchase exists
    pending_info = await db.get_pending_subscription_purchase(purchase_id)
    assert pending_info is not None
    assert pending_info["status"] == "pending"

    # 4. Admin approves subscription
    activated = await db.activate_user_subscription(purchase_id)
    assert activated is not None
    assert activated["telegram_id"] == tg_id
    assert activated["hours"] == 5

    # 5. Check profile after subscription
    prof_resp = await client.get(f"/profile/{tg_id}")
    assert prof_resp.status_code == 200
    prof_data = prof_resp.json()
    assert prof_data["subscription"]["has_subscription"] is True
    assert prof_data["subscription"]["available_hours"] == 5.0
    assert len(prof_data["subscription"]["active_subscriptions"]) == 1

    # 6. Check get-full-price for 2 slots (2 hours needed)
    gfp_payload = {
        "location": "Test 2",
        "day": "2026-11-20",
        "telegram_id": tg_id,
        "time_slots": {"10:00-11:00": 1, "11:00-12:00": 1}
    }
    gfp_resp = await client.post("/get-full-price", json=gfp_payload)
    assert gfp_resp.status_code == 200
    gfp_data = gfp_resp.json()
    order_id = gfp_data["temporary_order_id"]

    assert gfp_data["can_pay_with_subscription"] is True
    assert gfp_data["subscription"]["available_hours"] == 5.0
    assert gfp_data["subscription"]["required_hours"] == 2
    assert gfp_data["subscription"]["can_pay"] is True
    assert gfp_data["subscription"]["remaining_after_payment"] == 3.0

    # 7. Pay booking with subscription
    pay_payload = {
        "booking_id": order_id,
        "telegram_id": tg_id
    }
    pay_resp = await client.post("/pay-with-subscription", json=pay_payload)
    assert pay_resp.status_code == 200
    pay_data = pay_resp.json()
    assert pay_data["status"] == "success"
    assert pay_data["remaining_hours"] == 3.0

    # Check updated balance in profile
    prof_resp = await client.get(f"/profile/{tg_id}")
    prof_data = prof_resp.json()
    assert prof_data["subscription"]["available_hours"] == 3.0
    assert len(prof_data["bookings"]) >= 2

    # 8. Try to book 4 hours (only 3 available -> should indicate cannot pay)
    gfp_payload_4h = {
        "location": "Test 2",
        "day": "2026-11-21",
        "telegram_id": tg_id,
        "time_slots": {
            "12:00-13:00": 1,
            "13:00-14:00": 1,
            "14:00-15:00": 1,
            "15:00-16:00": 1
        }
    }
    gfp_resp_4h = await client.post("/get-full-price", json=gfp_payload_4h)
    assert gfp_resp_4h.status_code == 200
    gfp_data_4h = gfp_resp_4h.json()
    order_id_4h = gfp_data_4h["temporary_order_id"]

    assert gfp_data_4h["can_pay_with_subscription"] is False
    assert gfp_data_4h["subscription"]["can_pay"] is False
    assert gfp_data_4h["subscription"]["required_hours"] == 4
    assert gfp_data_4h["subscription"]["available_hours"] == 3.0

    # 9. If client attempts to pay with subscription when hours not enough -> 400 error
    pay_resp_fail = await client.post("/pay-with-subscription", json={
        "booking_id": order_id_4h,
        "telegram_id": tg_id
    })
    assert pay_resp_fail.status_code == 400
    assert "Недостаточно часов" in pay_resp_fail.json()["error"]
