import logging
from typing import Optional, List
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status, Body
from fastapi.responses import JSONResponse
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

from database.database import db
from telegram_bot.bot_app import bot
from telegram_bot.keyboards import keyboard
from config import config, models
from utils import nanoid_generate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


# ──────────────────────────────────────────────────────────────────────────
#  КЛИЕНТСКАЯ ЧАСТЬ: ПОЛУЧЕНИЕ И ПОКУПКА АБОНЕМЕНТОВ
# ──────────────────────────────────────────────────────────────────────────

@router.get("", summary="Список доступных для покупки абонементов")
@router.get("/available", summary="Список доступных для покупки абонементов")
async def get_available_subscriptions():
    """
    Возвращает список всех активных абонементов, которые клиенты могут купить в мини-аппе.
    """
    try:
        subs = await db.get_active_subscriptions()
        return JSONResponse(status_code=status.HTTP_200_OK, content=subs)
    except Exception as e:
        logger.exception("Error in get_available_subscriptions: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/my/{telegram_id}", summary="Абонементы и баланс часов пользователя")
async def get_my_subscriptions(telegram_id: int):
    """
    Возвращает текущие активные абонементы пользователя и суммарный остаток часов.
    """
    try:
        available_hours = await db.get_user_available_subscription_hours(telegram_id)
        active_subs = await db.get_user_active_subscriptions(telegram_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "telegram_id": telegram_id,
                "has_active": available_hours > 0,
                "available_hours": available_hours,
                "subscriptions": active_subs
            }
        )
    except Exception as e:
        logger.exception("Error in get_my_subscriptions for %s: %s", telegram_id, e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/buy", summary="Отправка чека об оплате абонемента")
@router.post("/buy-subscription", summary="Отправка чека об оплате абонемента (алиас)")
async def buy_subscription(
    photo: UploadFile = File(..., description="Скриншот / фото чека оплаты"),
    subscription_id: int = Form(..., description="ID выбранного тарифа абонемента"),
    telegram_id: Optional[int] = Form(None, description="Telegram ID пользователя"),
    id: Optional[int] = Form(None, description="Алиас для telegram_id"),
    price: Optional[str] = Form(None, description="Сумма оплаты (опционально)"),
):
    """
    Принимает чек оплаты абонемента, сохраняет заявку в pending_subscription_purchases
    и отправляет фото чека с информацией администратору в Telegram с кнопками подтверждения/отклонения.
    """
    user_id = telegram_id or id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user telegram id")

    sub = await db.get_subscription_by_id(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if not sub.get("is_active"):
        raise HTTPException(status_code=400, detail="This subscription is currently inactive")

    try:
        purchase_id = nanoid_generate()
        sub_price = float(price) if price else float(sub["price"])

        # Сохраняем заявку в БД
        await db.create_pending_subscription_purchase(
            purchase_id=purchase_id,
            telegram_id=user_id,
            subscription_id=subscription_id,
            price=sub_price
        )

        user_info = await db.get_user_data_by_id(user_id)
        phone, name = ("Неизвестно", "Неизвестно") if not user_info else (user_info[0], user_info[1])

        days_text = f"{sub['days_valid']} дн." if sub.get("days_valid") else "бессрочно"
        caption = (
            f"🎟 <b>ПОКУПКА АБОНИМЕНТА</b>\n"
            f"<b>Purchase ID</b>: <code>{purchase_id}</code>\n"
            f"📋 <b>ID</b> #{user_id}\n"
            f"👤 <b>Имя</b>: {name}\n"
            f"📞 <b>Номер</b>: {phone}\n"
            f"📦 <b>Абонемент</b>: {sub['name']}\n"
            f"⏳ <b>Количество часов</b>: {sub['hours']} ч.\n"
            f"💰 <b>Стоимость</b>: {sub_price:,.0f} сум\n"
            f"📅 <b>Срок действия</b>: {days_text}"
        )

        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_sub_ok_{purchase_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm_sub_no_{purchase_id}")
        ]])

        photo_bytes = await photo.read()
        filename = photo.filename or "receipt.jpg"
        input_file = BufferedInputFile(photo_bytes, filename=filename)

        admin_chat_id = config.ADMIN_ID
        await bot.send_photo(
            chat_id=admin_chat_id,
            photo=input_file,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

        logger.info("Subscription purchase %s sent to admin for user %s", purchase_id, user_id)
        return {
            "success": True,
            "purchase_id": purchase_id,
            "message": "Чек на покупку абонемента принят и отправлен администратору на проверку"
        }

    except Exception as e:
        logger.exception("Error in buy_subscription for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pay-booking", summary="Оплата слотов брони часами абонемента")
async def pay_booking_with_subscription(body: models.PayWithSubscriptionBody):
    """
    Подтверждает бронирование корта и оплачивает его часами с баланса абонемента.
    Списывает часы по FIFO, регистрирует бронь в календаре и БД, отправляет уведомления.
    """
    try:
        user_info = await db.get_user_data_by_id(body.telegram_id)
        phone, name = ("Неизвестно", "Неизвестно") if not user_info else (user_info[0], user_info[1])
        title = f"{name} | {phone} (Абонемент)"

        res = await db.confirm_subscription_booking(
            telegram_id=body.telegram_id,
            order_id=body.booking_id,
            title=title
        )

        if res["status"] == "not_found":
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "Бронь не найдена или истекло время холдирования"}
            )

        if res["status"] == "already":
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "already", "message": "Бронь уже была подтверждена ранее"}
            )

        if res["status"] == "not_enough_hours":
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "Недостаточно часов на абонементе",
                    "available_hours": res["available"],
                    "required_hours": res["required"]
                }
            )

        # Уведомляем пользователя на его языке
        lang = await db.get_lang(body.telegram_id) or "ru"
        time_slots_str = ", ".join(res["time_slots"])
        user_msg = {
            "ru": (
                f"✅ <b>Бронь успешно оплачена абонементом!</b>\n\n"
                f"📍 Локация: {res['location']}\n"
                f"📅 Дата: {res['date']}\n"
                f"⏰ Слоты: {time_slots_str}\n"
                f"⏳ Списано: {res['required_hours']} ч.\n"
                f"🎟 Остаток на абонементе: {res['remaining_hours']} ч.\n\n"
                f"Ждем вас на корте! 🎾"
            ),
            "en": (
                f"✅ <b>Booking confirmed with subscription!</b>\n\n"
                f"📍 Location: {res['location']}\n"
                f"📅 Date: {res['date']}\n"
                f"⏰ Slots: {time_slots_str}\n"
                f"⏳ Deducted: {res['required_hours']} hrs\n"
                f"🎟 Remaining hours: {res['remaining_hours']} hrs\n\n"
                f"See you on the court! 🎾"
            ),
            "uz": (
                f"✅ <b>Bron abonement orqali muvaffaqiyatli to‘landi!</b>\n\n"
                f"📍 Lokatsiya: {res['location']}\n"
                f"📅 Sana: {res['date']}\n"
                f"⏰ Vaqtlar: {time_slots_str}\n"
                f"⏳ Yechildi: {res['required_hours']} soat\n"
                f"🎟 Qolgan soatlar: {res['remaining_hours']} soat\n\n"
                f"Kortda kutamiz! 🎾"
            ),
            "uz-cyr": (
                f"✅ <b>Брон абонемент орқали муваффақиятли тўланди!</b>\n\n"
                f"📍 Локация: {res['location']}\n"
                f"📅 Сана: {res['date']}\n"
                f"⏰ Вақтлар: {time_slots_str}\n"
                f"⏳ Ечилди: {res['required_hours']} соат\n"
                f"🎟 Қолган соатлар: {res['remaining_hours']} соат\n\n"
                f"Кортда кутамиз! 🎾"
            )
        }

        try:
            await bot.send_message(
                chat_id=body.telegram_id,
                text=user_msg.get(lang, user_msg["ru"]),
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning("Failed to notify user of subscription booking: %s", e)

        # Уведомляем администратора о новом бронировании
        try:
            admin_msg = (
                f"🎟 <b>Новая бронь по абонементу!</b>\n\n"
                f"📋 <b>Booking ID</b>: <code>{body.booking_id}</code>\n"
                f"👤 <b>Имя</b>: {name}\n"
                f"📞 <b>Номер</b>: {phone} (ID: #{body.telegram_id})\n"
                f"📍 <b>Локация</b>: {res['location']}\n"
                f"📅 <b>Дата</b>: {res['date']}\n"
                f"⏰ <b>Слоты</b>: {time_slots_str}\n"
                f"⏳ <b>Списано</b>: {res['required_hours']} ч.\n"
                f"🎟 <b>Остаток у клиента</b>: {res['remaining_hours']} ч."
            )
            await bot.send_message(chat_id=config.ADMIN_ID, text=admin_msg, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify admin of subscription booking: %s", e)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "success",
                "message": "Бронь успешно оплачена абонементом",
                "order_id": body.booking_id,
                "remaining_hours": res["remaining_hours"]
            }
        )
    except Exception as e:
        logger.exception("Error in pay_booking_with_subscription: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ──────────────────────────────────────────────────────────────────────────
#  АДМИНИСТРАТИВНАЯ ЧАСТЬ: ПОЛУЧЕНИЕ И ИЗМЕНЕНИЕ АБОНЕМЕНТОВ
# ──────────────────────────────────────────────────────────────────────────

@router.get("/admin/all", summary="Админ: получение всех абонементов")
async def get_all_subscriptions_admin():
    """
    Возвращает абсолютно все тарифы абонементов (включая отключенные) для панели управления.
    """
    try:
        subs = await db.get_all_subscriptions()
        return JSONResponse(status_code=status.HTTP_200_OK, content=subs)
    except Exception as e:
        logger.exception("Error in get_all_subscriptions_admin: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/admin/create", summary="Админ: создание нового тарифа абонемента")
async def create_subscription_admin(body: models.CreateSubscriptionBody):
    """
    Создание нового тарифа абонемента (название, количество часов, цена, описание, дни действия).
    """
    try:
        sub_id = await db.create_subscription(
            name=body.name,
            hours=body.hours,
            price=body.price,
            description=body.description,
            days_valid=body.days_valid,
            is_active=body.is_active if body.is_active is not None else 1
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"status": "created", "id": sub_id, "message": "Абонемент успешно создан"}
        )
    except Exception as e:
        logger.exception("Error in create_subscription_admin: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.put("/admin/{subscription_id}", summary="Админ: изменение тарифа абонемента")
@router.post("/admin/update", summary="Админ: изменение тарифа абонемента (алиас POST)")
async def update_subscription_admin(
    subscription_id: Optional[int] = None,
    body: models.UpdateSubscriptionBody = Body(...),
    id: Optional[int] = None
):
    """
    Изменение параметров существующего тарифа абонемента (цена, часы, активность и т.д.).
    """
    target_id = subscription_id or body.id or id
    if not target_id:
        raise HTTPException(status_code=400, detail="Missing subscription id")

    try:
        success = await db.update_subscription(
            sub_id=target_id,
            name=body.name,
            hours=body.hours,
            price=body.price,
            description=body.description,
            days_valid=body.days_valid,
            is_active=body.is_active
        )
        if not success:
            raise HTTPException(status_code=404, detail="Subscription not found")

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "updated", "id": target_id, "message": "Абонемент успешно обновлен"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in update_subscription_admin: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.delete("/admin/{subscription_id}", summary="Админ: удаление тарифа абонемента")
async def delete_subscription_admin(subscription_id: int):
    """
    Удаление тарифа абонемента по ID.
    """
    try:
        success = await db.delete_subscription(subscription_id)
        if not success:
            raise HTTPException(status_code=404, detail="Subscription not found")

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "deleted", "id": subscription_id, "message": "Абонемент успешно удален"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in delete_subscription_admin: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})

