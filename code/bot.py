import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from database import SessionLocal, Question, FAQ, ResponseRating, Log
from ollama_handler import OllamaHandler
import datetime
import asyncio
import functools
import re
import hashlib

TOKEN = "7795035095:AAFgeRJU8ZriQ-BCN4Ew_0YE7wttAYnZw2o"

# Создаем единственный экземпляр OllamaHandler при запуске приложения
ollama_instance = OllamaHandler()

# Временное хранилище вопросов (для предотвращения проблем с длиной callback_data)
# Формат: {question_hash: question_text}
temp_questions = {}


def get_question_hash(question_text):
    """Генерирует короткий хеш для вопроса"""
    # Создаем MD5-хеш и берем первые 8 символов
    return hashlib.md5(question_text.encode('utf-8')).hexdigest()[:8]


def save_temp_question(question_text):
    """Сохраняет вопрос во временное хранилище и возвращает его хеш"""
    question_hash = get_question_hash(question_text)
    temp_questions[question_hash] = question_text
    return question_hash


def find_faq_answer(question):
    session = SessionLocal()
    try:
        # Нормализуем входящий вопрос
        normalized_question = question.lower().strip()

        # Получаем все FAQ записи
        faqs = session.query(FAQ).all()

        best_match = None
        highest_ratio = 0

        for faq in faqs:
            # Нормализуем вопрос из FAQ
            faq_question = faq.question.lower().strip()

            # Проверяем различные варианты совпадений

            # 1. Прямое совпадение
            if normalized_question == faq_question:
                return faq.answer

            # 2. Содержит ли FAQ вопрос ключевые слова из запроса
            words = normalized_question.split()
            matched_words = sum(1 for word in words if word in faq_question)
            ratio = matched_words / len(words) if words else 0

            # 3. Проверяем на опечатки и похожие слова
            from difflib import SequenceMatcher
            similarity = SequenceMatcher(None, normalized_question, faq_question).ratio()

            # 4. Проверяем, содержится ли запрос в вопросе FAQ
            contains_ratio = 1.0 if normalized_question in faq_question else 0

            # Вычисляем общий коэффициент схожести
            total_ratio = max(ratio, similarity, contains_ratio)

            # Обновляем лучшее совпадение
            if total_ratio > highest_ratio and total_ratio > 0.4:  # Порог схожести 40%
                highest_ratio = total_ratio
                best_match = faq.answer

        return best_match

    finally:
        session.close()


def send_message_to_user(chat_id, message, reply_markup=None):
    """Отправка сообщения пользователю"""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message,
    }

    if reply_markup:
        data["reply_markup"] = reply_markup.to_dict()

    try:
        response = requests.post(url, json=data)
        if response.status_code != 200:
            print(f"Ошибка отправки сообщения: {response.text}")
    except Exception as e:
        print(f"Ошибка при отправке сообщения: {e}")


def notify_user_about_response(question_id):
    """Уведомление пользователя о новом ответе"""
    session = SessionLocal()
    try:
        question = session.get(Question, question_id)
        if question and question.status == "отвечен" and question.response:
            message = (
                f"✅ Получен ответ на ваш вопрос!\n"
                f"❓ Ваш вопрос:\n{question.question}\n"
                f"📝 Ответ:\n{question.response}"
            )

            keyboard = [
                [
                    InlineKeyboardButton("❓ Задать новый вопрос", callback_data="ask_question"),
                    InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
                ],
                [
                    InlineKeyboardButton("👍", callback_data=f"rate_up_human_{question_id}"),
                    InlineKeyboardButton("👎", callback_data=f"rate_down_human_{question_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            send_message_to_user(question.user_id, message, reply_markup)
    except Exception as e:
        print(f"Ошибка при отправке уведомления: {e}")
    finally:
        session.close()


def update_question_status(question_id, new_status, response=None):
    """Обновление статуса вопроса и отправка уведомления"""
    session = SessionLocal()
    try:
        question = session.get(Question, question_id)
        if question:
            old_status = question.status
            question.status = new_status
            if response:
                question.response = response

            # Логируем изменения
            log_entry = Log(
                user_id=0,  # Системное действие
                question_id=question_id,
                action=f"Автоматическое обновление статуса с '{old_status}' на '{new_status}'",
                action_type='status_change',
                timestamp=datetime.datetime.now()
            )
            session.add(log_entry)
            session.commit()

            if new_status == "отвечен":
                notify_user_about_response(question_id)
    except Exception as e:
        session.rollback()
        print(f"Ошибка при обновлении статуса: {e}")
    finally:
        session.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начальное приветствие и главное меню"""
    keyboard = [
        [
            InlineKeyboardButton("❓ Задать вопрос", callback_data="ask_question"),
            InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "👋 Добро пожаловать в службу поддержки!\n\n"
        "🤝 Я помогу вам получить ответы на ваши вопросы.\n"
        "Выберите действие из меню ниже:",
        reply_markup=reply_markup
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    # Удаляем только кнопки из сообщения, из которого они были нажаты
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        print(f"Ошибка при удалении кнопок: {e}")

    if query.data == "ask_question":
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = await query.message.reply_text(
            "📝 Пожалуйста, опишите ваш вопрос.\n"
            "Постарайтесь указать всю необходимую информацию.",
            reply_markup=reply_markup
        )
        # Сохраняем ID сообщения с инструкцией
        context.user_data['instruction_message_id'] = message.message_id
        context.user_data['waiting_for_question'] = True

    elif query.data == "my_questions":
        await show_user_questions(update, context)

    elif query.data.startswith("view_question_"):
        await show_question_details(update, context)

    elif query.data == "cancel":
        context.user_data['waiting_for_question'] = False
        keyboard = [
            [
                InlineKeyboardButton("❓ Задать вопрос", callback_data="ask_question"),
                InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            "❌ Отменено. Выберите действие:",
            reply_markup=reply_markup
        )
    elif query.data.startswith("not_helpful_"):
        # Извлекаем хеш вопроса вместо полного текста
        question_hash = query.data.replace("not_helpful_", "")
        # Получаем текст вопроса из временного хранилища
        original_question = temp_questions.get(question_hash, "Вопрос не найден")

        session = SessionLocal()
        try:
            new_question = Question(
                user_id=query.from_user.id,
                question=original_question,
                status="в обработке",
                priority=2  # Повышенный приоритет
            )
            session.add(new_question)
            session.commit()

            keyboard = [
                [
                    InlineKeyboardButton("❓ Задать другой вопрос", callback_data="ask_question"),
                    InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.message.reply_text(
                "🙏 Извините за неточный ответ!\n"
                "✅ Ваш вопрос передан специалистам поддержки.\n"
                "⏳ Мы ответим вам в ближайшее время.",
                reply_markup=reply_markup
            )
        finally:
            session.close()
    elif query.data.startswith("rate_"):
        # Парсим данные из callback_data
        parts = query.data.split("_")
        rating_type = parts[1]  # up или down
        source = parts[2]  # faq, ai или human

        # Если это оценка ответа от человека, то parts[3] - это ID вопроса
        # Иначе parts[3] - это хеш вопроса
        if source == "human":
            question_id = int(parts[3])
            question_text = None
        else:
            question_id = None
            # Получаем текст вопроса из временного хранилища по хешу
            question_hash = parts[3]
            question_text = temp_questions.get(question_hash, "Вопрос не найден")

        # Сохраняем оценку в базе данных
        session = SessionLocal()
        try:
            # Если это оценка ответа от человека, то у нас есть question_id
            if question_id:
                rating = ResponseRating(
                    question_id=question_id,
                    user_id=query.from_user.id,
                    rating=1 if rating_type == "up" else 0,
                    source=source
                )
            # Иначе создаем запись только с текстом вопроса
            else:
                rating = ResponseRating(
                    user_id=query.from_user.id,
                    rating=1 if rating_type == "up" else 0,
                    source=source
                )

            session.add(rating)
            session.commit()

            # Отправляем подтверждение пользователю с новыми кнопками
            keyboard = [
                [
                    InlineKeyboardButton("❓ Задать новый вопрос", callback_data="ask_question"),
                    InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.message.reply_text(
                "Спасибо за вашу оценку! Это помогает нам улучшать качество ответов.",
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Ошибка при сохранении оценки: {e}")
        finally:
            session.close()


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_question'):
        return

    if 'instruction_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['instruction_message_id']
            )
            del context.user_data['instruction_message_id']
        except Exception as e:
            print(f"Ошибка при удалении инструкции: {e}")

    user_id = update.message.from_user.id
    question_text = update.message.text.strip()

    # Сохраняем вопрос во временное хранилище и получаем его хеш
    question_hash = save_temp_question(question_text)

    # 1. Проверяем наличие ответа в FAQ
    faq_answer = find_faq_answer(question_text)
    if faq_answer:
        keyboard = [
            [
                InlineKeyboardButton("❓ Задать другой вопрос", callback_data="ask_question"),
                InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
            ],
            [InlineKeyboardButton("⚠️ Этот ответ мне не помог", callback_data=f"not_helpful_{question_hash}")],
            [
                InlineKeyboardButton("👍", callback_data=f"rate_up_faq_{question_hash}"),
                InlineKeyboardButton("👎", callback_data=f"rate_down_faq_{question_hash}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Отправляем ответ без Markdown-форматирования
        await update.message.reply_text(
            f"🔍 Найден похожий ответ:\n\n{faq_answer}",
            reply_markup=reply_markup
        )
        context.user_data['waiting_for_question'] = False
        return

    # 2. Если ответ не найден в FAQ, используем Ollama
    # Отправляем промежуточное сообщение о том, что ответ генерируется
    processing_message = await update.message.reply_text(
        "🤖 Генерирую ответ на ваш вопрос... Это может занять несколько секунд."
    )

    # Определяем функцию обратного вызова для асинхронной обработки
    async def process_ai_response(user_id, question_text, processing_message_id):
        # Функция обратного вызова для обработки результата от Ollama
        def on_response_ready(ai_response):
            asyncio.run_coroutine_threadsafe(
                send_ai_response(user_id, question_text, ai_response, processing_message_id),
                context.application.loop
            )

        # Используем глобальный экземпляр Ollama в синхронном режиме
        # (так как вызываем из асинхронной функции)
        ai_response = ollama_instance.generate_response(question_text)
        await send_ai_response(user_id, question_text, ai_response, processing_message_id)

    # Асинхронная функция для отправки ответа от ИИ
    async def send_ai_response(user_id, question_text, ai_response, processing_message_id):
        # Пытаемся удалить промежуточное сообщение
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=processing_message_id
            )
        except Exception as e:
            print(f"Ошибка при удалении промежуточного сообщения: {e}")

        if ai_response:
            # Используем хеш вопроса вместо полного текста
            keyboard = [
                [
                    InlineKeyboardButton("❓ Задать другой вопрос", callback_data="ask_question"),
                    InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
                ],
                [InlineKeyboardButton("⚠️ Этот ответ мне не помог", callback_data=f"not_helpful_{question_hash}")],
                [
                    InlineKeyboardButton("👍", callback_data=f"rate_up_ai_{question_hash}"),
                    InlineKeyboardButton("👎", callback_data=f"rate_down_ai_{question_hash}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Отправляем сообщение без форматирования
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🤖 Ответ ИИ:\n\n{ai_response}",
                reply_markup=reply_markup
            )
            context.user_data['waiting_for_question'] = False
        else:
            # 3. Если ИИ не смог дать ответ, сохраняем вопрос для админов
            session = SessionLocal()
            try:
                new_question = Question(
                    user_id=user_id,
                    question=question_text,
                    status="в обработке"
                )
                session.add(new_question)
                session.commit()

                keyboard = [
                    [
                        InlineKeyboardButton("❓ Задать ещё вопрос", callback_data="ask_question"),
                        InlineKeyboardButton("📋 Мои вопросы", callback_data="my_questions")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="✅ Ваш вопрос зарегистрирован!\n"
                         "⏳ Наши специалисты ответят вам в ближайшее время.\n"
                         "🔔 Вы получите уведомление, когда появится ответ.",
                    reply_markup=reply_markup
                )
            except Exception as e:
                print(f"Ошибка при сохранении вопроса: {e}")
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ Произошла ошибка при сохранении вопроса. Пожалуйста, попробуйте позже."
                )
            finally:
                session.close()
                context.user_data['waiting_for_question'] = False

    # Запускаем обработку ответа ИИ
    await process_ai_response(user_id, question_text, processing_message.message_id)


async def show_user_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список вопросов пользователя"""
    query = update.callback_query
    user_id = query.from_user.id

    session = SessionLocal()
    try:
        # Добавляем фильтр is_archived == 0 для показа только неархивированных вопросов
        questions = session.query(Question).filter(
            Question.user_id == user_id,
            Question.is_archived == 0
        ).order_by(Question.id.desc()).all()

        if not questions:
            keyboard = [
                [
                    InlineKeyboardButton("❓ Задать вопрос", callback_data="ask_question")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text(
                "У вас пока нет активных вопросов.",
                reply_markup=reply_markup
            )
            return

        keyboard = []
        for q in questions:
            status = "✅" if q.status == "отвечен" else "⏳"
            button_text = f"{status} {q.question[:30]}..."
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_question_{q.id}")])

        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.message.reply_text(
            "Ваши активные вопросы:",
            reply_markup=reply_markup
        )
    finally:
        session.close()


async def show_question_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детали конкретного вопроса"""
    query = update.callback_query
    await query.answer()

    # Получаем ID вопроса из callback_data
    question_id = int(query.data.split('_')[2])

    session = SessionLocal()
    try:
        question = session.get(Question, question_id)
        if question:
            status_emoji = "✅" if question.status == "отвечен" else "⏳"
            status_text = "Отвечен" if question.status == "отвечен" else "В обработке"

            priority_icons = {1: "🟢", 2: "🟡", 3: "🔴"}
            priority_text = {1: "Низкий", 2: "Средний", 3: "Высокий"}

            message_text = (
                f"Вопрос #{question.id}\n\n"
                f"📝 {question.question}\n\n"
                f"Статус: {status_emoji} {status_text}\n"
                f"Приоритет: {priority_icons[question.priority]} {priority_text[question.priority]}\n"
            )

            if question.response:
                message_text += f"\nОтвет: {question.response}"

            keyboard = [[InlineKeyboardButton("🔙 К списку вопросов", callback_data="my_questions")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.message.reply_text(
                message_text,
                reply_markup=reply_markup
            )
        else:
            await query.message.reply_text("Вопрос не найден.")
    finally:
        session.close()


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Обработчики команд и сообщений
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(CallbackQueryHandler(handle_button))

    print("🤖 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
