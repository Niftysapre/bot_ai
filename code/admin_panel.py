from flask import Flask, render_template, request, redirect, flash, url_for, jsonify
import math
import requests
import threading  # Новый импорт для асинхронной отправки
from auth import auth, login_manager
from flask_login import login_required, current_user
from utils import log_action
import datetime
from sqlalchemy.sql import func
from werkzeug.security import generate_password_hash
from bot import update_question_status, notify_user_about_response
from functools import wraps  # Добавим в начало файла

# Импорт моделей и сессии SQLAlchemy
from database import SessionLocal, Question, User, Log, AnswerHistory, FAQ, ResponseRating

def combine(dict1, dict2):
    new_dict = dict1.copy() if dict1 else {}
    new_dict.update(dict2)
    return new_dict

# Функция для преобразования времени в московское (UTC+3)
def to_moscow_time(dt):
    """Конвертирует datetime из UTC в московское время (UTC+3)"""
    if not dt:
        return dt
    # Добавляем 3 часа к времени
    return dt + datetime.timedelta(hours=3)

app = Flask(__name__)
app.secret_key = "секретный_ключ"
app.register_blueprint(auth)
login_manager.init_app(app)

# Регистрируем кастомные фильтры для шаблонов
app.jinja_env.filters['combine'] = combine
app.jinja_env.filters['to_moscow_time'] = to_moscow_time


# Добавляем декоратор для проверки ролей
def requires_role(*roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.role in roles:
                flash("У вас нет доступа к этой странице.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)

        return wrapped

    return decorator


# Эндпоинт для главной страницы (список активных вопросов)
@app.route("/")
@login_required
@requires_role("admin", "moderator", "superadmin")
def index():
    session = SessionLocal()
    try:
        # Базовый запрос для активных вопросов
        query = session.query(Question).filter(Question.is_archived == 0)

        # Применяем фильтры
        search = request.args.get('search', '').strip()
        status = request.args.get('status', '').strip()
        priority = request.args.get('priority', '').strip()

        if search:
            query = query.filter(Question.question.ilike(f'%{search}%'))
        if status:
            query = query.filter(Question.status == status)
        if priority and priority.isdigit():
            query = query.filter(Question.priority == int(priority))

        # Сначала сортируем по приоритету (по убыванию), затем по ID
        query = query.order_by(Question.priority.desc(), Question.id.desc())

        # Пагинация
        page = request.args.get('page', 1, type=int)
        per_page = 10

        questions = query.offset((page - 1) * per_page).limit(per_page).all()
        total = query.count()
        total_pages = math.ceil(total / per_page)
        users = session.query(User).all()

        return render_template(
            "index.html",
            questions=questions,
            page=page,
            total_pages=total_pages,
            users=users
        )
    finally:
        session.close()


# Редактирование вопроса (GET – форма редактирования, POST – сохранение изменений)
@app.route("/edit/<int:question_id>", methods=["GET", "POST"])
@login_required
@requires_role("admin", "moderator", "superadmin")
def edit_question(question_id):
    session = SessionLocal()
    try:
        question = session.get(Question, question_id)
        logs = session.query(Log).filter(
            Log.question_id == question_id
        ).order_by(Log.timestamp.desc()).all()

        if request.method == "POST":
            action = request.form.get("action", "save")
            new_status = request.form.get("status")
            new_response = request.form.get("response", "").strip()
            new_priority = int(request.form.get("priority", 1))

            # Собираем список изменений
            changes = []

            if action == "send":
                new_status = "отвечен"

            # Проверяем, что реально изменилось
            if question.status != new_status:
                changes.append(f"📝 Статус: {question.status} ➜ {new_status}")

            if question.priority != new_priority:
                priority_icons = {1: "🟢", 2: "🟡", 3: "🔴"}
                old_priority_text = f"{priority_icons[question.priority]} {question.priority}"
                new_priority_text = f"{priority_icons[new_priority]} {new_priority}"
                changes.append(f"⚡ Приоритет: {old_priority_text} ➜ {new_priority_text}")

            if question.response != new_response:
                old_response = question.response or "не заполнено"
                changes.append(
                    f"💬 Ответ:\n"
                    f"До: {old_response[:100]}{'...' if len(old_response) > 100 else ''}\n"
                    f"После: {new_response[:100]}{'...' if len(new_response) > 100 else ''}"
                )

            # Обновляем поля вопроса
            question.status = new_status
            question.response = new_response
            question.priority = new_priority

            # Записываем в лог только если были изменения
            if changes:
                log_entry = Log(
                    user_id=current_user.id,
                    question_id=question_id,
                    action="\n".join(changes),
                    action_type='edit',
                    timestamp=datetime.datetime.now()
                )
                session.add(log_entry)

            session.commit()

            if new_status == "отвечен":
                notify_user_about_response(question_id)

            flash("Изменения сохранены успешно!", "success")
            return redirect(url_for("index"))

        return render_template(
            "edit.html",
            q=question,
            logs=logs,
            now=datetime.datetime.now()
        )
    except Exception as e:
        session.rollback()
        flash(f"Ошибка при редактировании: {str(e)}", "danger")
        return redirect(url_for("index"))
    finally:
        session.close()


# Восстановление вопроса (из архива)
@app.route("/questions/restore/<int:question_id>", methods=["POST"])
@login_required
@requires_role("admin", "moderator", "superadmin")
def restore_question(question_id):
    session = SessionLocal()
    try:
        q_obj = session.query(Question).filter(Question.id == question_id).first()
        if q_obj:
            q_obj.is_archived = 0
            log_entry = Log(
                user_id=current_user.id,
                question_id=question_id,
                action=f"Вопрос ID {question_id} был восстановлен из архива",
                action_type='restore',
                timestamp=datetime.datetime.now()
            )
            session.add(log_entry)
            session.commit()
            flash("Вопрос успешно восстановлен.", "success")
        else:
            flash("Вопрос не найден.", "danger")
    finally:
        session.close()
    return redirect(url_for("archive"))


# Удаление вопроса
@app.route("/questions/delete/<int:question_id>", methods=["POST"])
@login_required
@requires_role("admin", "moderator", "superadmin")
def delete_question(question_id):
    session = SessionLocal()
    try:
        q_obj = session.query(Question).filter(Question.id == question_id).first()
        if not q_obj:
            flash("Вопрос не найден.", "danger")
            return redirect(url_for("archive"))

        # Сначала удаляем все связанные логи
        session.query(Log).filter(Log.question_id == question_id).delete()

        # Затем удаляем сам вопрос
        question_text = q_obj.question
        session.delete(q_obj)

        # Создаем новый лог об удалении (без привязки к вопросу)
        log_entry = Log(
            user_id=current_user.id,
            action=f"Вопрос ID {question_id} был удалён",
            action_type='delete',
            timestamp=datetime.datetime.now()
        )
        session.add(log_entry)
        session.commit()
        flash("Вопрос успешно удалён.", "success")
    finally:
        session.close()
    return redirect(url_for("archive"))


# Управление пользователями (список пользователей с пагинацией)
@app.route("/users")
@login_required
@requires_role("admin", "superadmin")
def manage_users():
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    per_page = 10
    offset = (page - 1) * per_page

    session = SessionLocal()
    try:
        users = session.query(User).order_by(
            User.id.desc()
        ).limit(per_page).offset(offset).all()

        total = session.query(User).count()
        total_pages = math.ceil(total / per_page)

        return render_template(
            "users.html",
            users=users,
            page=page,
            total_pages=total_pages
        )
    finally:
        session.close()


# Отзыв прав у пользователя
@app.route("/users/revoke/<int:user_id>", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def revoke_user(user_id):
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if user and user.role == "admin" and current_user.role != "superadmin":
            flash("Нельзя отозвать доступ у администратора.", "danger")
            return redirect(url_for("manage_users"))
        if user and user.role == "superadmin" and current_user.role != "superadmin":
            flash("Нельзя отозвать доступ у суперадминистратора.", "danger")
            return redirect(url_for("manage_users"))
        if user:
            user.is_active = 0
            user.role = 'user'
            session.commit()
    finally:
        session.close()
    return redirect(url_for("manage_users"))


# Удаление пользователя
@app.route("/users/delete/<int:user_id>", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def delete_user(user_id):
    if user_id == current_user.id:
        flash("Вы не можете удалить свою учетную запись.", "danger")
        return redirect(url_for("manage_users"))

    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            flash("Пользователь не найден.", "danger")
            return redirect(url_for("manage_users"))

        # Проверки для суперадмина
        if current_user.role == "superadmin":
            # Суперадмин может удалить любого, кроме себя
            pass
        else:
            # Админ не может удалить суперадмина
            if user.role == "superadmin":
                flash("Вы не можете удалить суперадмина.", "danger")
                return redirect(url_for("manage_users"))

            # Админ не может удалить другого админа
            if user.role == "admin":
                flash("Вы не можете удалить другого администратора.", "danger")
                return redirect(url_for("manage_users"))

        username = user.username
        role = user.role
        session.delete(user)
        session.commit()

        log_action(
            f"Удален пользователь '{username}' с ролью '{role}'",
            action_type='user_delete'
        )

        flash(f"Пользователь {username} успешно удален.", "success")
    except Exception as e:
        session.rollback()
        flash(f"Ошибка при удалении пользователя: {str(e)}", "danger")
    finally:
        session.close()

    return redirect(url_for("manage_users"))


# Одобрение пользователя
@app.route("/users/approve/<int:user_id>", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def approve_user(user_id):
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if user:
            user.is_active = 1
            user.role = 'moderator'
            session.commit()
    finally:
        session.close()

    return redirect(url_for("manage_users"))


# Архивация вопроса
@app.route("/questions/archive/<int:question_id>", methods=["POST"])
@login_required
@requires_role("admin", "moderator", "superadmin")
def archive_question(question_id):
    session = SessionLocal()
    try:
        q_obj = session.query(Question).filter(Question.id == question_id).first()
        if q_obj:
            q_obj.is_archived = 1
            log_entry = Log(
                user_id=current_user.id,
                question_id=question_id,
                action=f"Вопрос ID {question_id} был перемещен в архив",
                action_type='archive',
                timestamp=datetime.datetime.now()
            )
            session.add(log_entry)
            session.commit()
            flash(f"Вопрос ID {question_id} успешно архивирован.", "success")
        else:
            flash("Вопрос не найден.", "danger")
    finally:
        session.close()
    return redirect(url_for("index"))


# Страница логов с пагинацией
@app.route("/logs")
@login_required
@requires_role("admin", "superadmin")
def logs_page():
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    per_page = 10
    offset = (page - 1) * per_page

    session = SessionLocal()
    try:
        logs = session.query(Log).order_by(
            Log.timestamp.desc()
        ).limit(per_page).offset(offset).all()

        total = session.query(Log).count()
        total_pages = math.ceil(total / per_page)

        return render_template(
            "logs.html",
            logs=logs,
            page=page,
            total_pages=total_pages
        )
    finally:
        session.close()


# Эндпоинт для возврата списка вопросов в формате JSON
@app.route("/questions_json")
@login_required
@requires_role("admin", "moderator", "superadmin")
def questions_json():
    search_query = request.args.get("search", "")
    status_filter = request.args.get("status", "")
    user_filter = request.args.get("user", "")
    priority_filter = request.args.get("priority", "")

    session = SessionLocal()
    try:
        query = session.query(Question).filter(Question.is_archived == 0)
        if search_query:
            query = query.filter(Question.question.like(f"%{search_query}%"))
        if status_filter:
            query = query.filter(Question.status == status_filter)
        if user_filter:
            query = query.filter(Question.user_id == user_filter)
        if priority_filter:
            query = query.filter(Question.priority == priority_filter)
        query = query.order_by(Question.priority.desc())
        questions = query.all()
    finally:
        session.close()

    questions_list = []
    for q in questions:
        questions_list.append({
            "id": q.id,
            "user_id": q.user_id,
            "question": q.question,
            "status": q.status,
            "response": q.response,
            "priority": q.priority
        })
    return jsonify(questions_list)


# Удаление всех архивных вопросов
@app.route("/questions/delete_all_archived", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def delete_all_archived():
    session = SessionLocal()
    try:
        # Получаем ID всех архивных вопросов перед удалением
        archived_questions = session.query(Question.id).filter(Question.is_archived == True).all()
        archived_ids = [q.id for q in archived_questions]
        count = len(archived_ids)

        if count > 0:
            # Удаляем логи для всех архивных вопросов
            session.query(Log).filter(Log.question_id.in_(archived_ids)).delete(synchronize_session=False)

            # Удаляем вопросы
            session.query(Question).filter(Question.is_archived == True).delete(synchronize_session=False)

            # Создаем запись в логах
            log_entry = Log(
                user_id=current_user.id,
                action=f"🗑️ Массовое удаление {count} архивированных вопросов (ID: {', '.join(map(str, archived_ids))})",
                action_type='delete',
                timestamp=datetime.datetime.now()
            )
            session.add(log_entry)
            session.commit()
            flash(f"Удалено {count} архивных вопросов.", "success")
        else:
            flash("Нет архивных вопросов для удаления.", "info")
    except Exception as e:
        session.rollback()
        flash(f"Ошибка при удалении архивных вопросов: {str(e)}", "danger")
    finally:
        session.close()
    return redirect(url_for("archive"))


# Страница FAQ
@app.route("/faq")
@login_required
@requires_role("admin", "moderator", "superadmin")
def faq():
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    per_page = 10
    offset = (page - 1) * per_page

    session = SessionLocal()
    try:
        faqs = session.query(FAQ).order_by(
            FAQ.id.desc()
        ).limit(per_page).offset(offset).all()

        total = session.query(FAQ).count()
        total_pages = math.ceil(total / per_page)

        return render_template(
            "faq.html",
            faqs=faqs,
            page=page,
            total_pages=total_pages
        )
    finally:
        session.close()


@app.route("/faq/add", methods=["GET", "POST"])
@login_required
@requires_role("admin", "superadmin")
def add_faq():
    if request.method == "POST":
        question_text = request.form.get("question").strip()
        answer_text = request.form.get("answer").strip()

        if question_text and answer_text:
            session = SessionLocal()
            try:
                new_faq = FAQ(question=question_text, answer=answer_text)
                session.add(new_faq)
                session.commit()
                flash("FAQ успешно добавлен.", "success")
                log_action(
                    action=f"FAQ ID: {new_faq.id} был добавлен, вопрос: {new_faq.question}",
                    action_type='faq_add'
                )
            except Exception as e:
                session.rollback()
                flash(f"Ошибка при добавлении FAQ: {str(e)}", "danger")
                log_action(
                    action=f"Ошибка при добавлении FAQ: {str(e)}",
                    action_type='faq_add_error'
                )

            finally:
                session.close()
            return redirect(url_for("faq"))
        else:
            flash("Введите вопрос и ответ.", "danger")

    return render_template("add_faq.html")


@app.route("/faq/edit/<int:faq_id>", methods=["GET", "POST"])
@login_required
@requires_role("admin", "superadmin")
def edit_faq(faq_id):
    session = SessionLocal()
    try:
        faq = session.query(FAQ).filter(FAQ.id == faq_id).first()
    finally:
        session.close()

    if not faq:
        flash("Вопрос не найден.", "danger")
        return redirect(url_for("faq"))

    if request.method == "POST":
        new_question = request.form.get("question").strip()
        new_answer = request.form.get("answer").strip()
        session = SessionLocal()
        try:
            faq_obj = session.query(FAQ).filter(FAQ.id == faq_id).first()
            if faq_obj:
                old_question = faq_obj.question
                old_answer = faq_obj.answer
                faq_obj.question = new_question
                faq_obj.answer = new_answer
                session.commit()
                flash("FAQ успешно обновлён.", "success")
                log_action(
                    action=f"FAQ ID: {faq_obj.id}. Старый вопрос: {old_question}; Новый вопрос: {faq_obj.question}",
                    action_type='faq_edit'
                )
            else:
                flash("Вопрос не найден.", "danger")
        except Exception as e:
            session.rollback()
            flash(f"Ошибка при редактировании FAQ: {str(e)}", "danger")
            log_action(
                action=f"Ошибка при редактировании FAQ: {str(e)}",
                action_type='faq_edit'
            )
        finally:
            session.close()
        return redirect(url_for("faq"))

    return render_template("edit_faq.html", faq=faq)


@app.route("/faq/delete/<int:faq_id>", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def delete_faq(faq_id):
    session = SessionLocal()
    try:
        faq = session.query(FAQ).filter(FAQ.id == faq_id).first()
        if faq:
            session.delete(faq)
            session.commit()
            flash("FAQ успешно удалён.", "success")
            log_action(
                action=f"FAQ ID: {faq_id} был удалён",
                action_type='faq_delete'
            )
        else:
            flash("Вопрос не найден.", "danger")
    except Exception as e:
        session.rollback()
        flash(f"Ошибка при удалении FAQ: {str(e)}", "danger")
        log_action(
            action=f"Ошибка при удалении FAQ: {str(e)}",
            action_type='faq_delete'
        )
    finally:
        session.close()
    return redirect(url_for("faq"))


@app.route("/archive")
@login_required
@requires_role("admin", "moderator", "superadmin")
def archive():
    session = SessionLocal()
    try:
        # Базовый запрос для архивированных вопросов
        query = session.query(Question).filter(Question.is_archived == True)

        # Применяем фильтры
        search = request.args.get('search', '').strip()
        status = request.args.get('status', '').strip()
        priority = request.args.get('priority', '').strip()

        if search:
            query = query.filter(Question.question.ilike(f'%{search}%'))
        if status:
            query = query.filter(Question.status == status)
        if priority and priority.isdigit():
            query = query.filter(Question.priority == int(priority))

        # Сначала сортируем по приоритету (по убыванию), затем по ID
        query = query.order_by(Question.priority.desc(), Question.id.desc())

        # Пагинация
        page = request.args.get('page', 1, type=int)
        per_page = 10

        questions = query.offset((page - 1) * per_page).limit(per_page).all()
        total = query.count()
        total_pages = math.ceil(total / per_page)
        users = session.query(User).all()

        return render_template(
            'archive.html',
            questions=questions,
            page=page,
            total_pages=total_pages,
            users=users
        )
    finally:
        session.close()


@app.route("/analytics")
@login_required
@requires_role("admin", "superadmin")
def analytics():
    session = SessionLocal()
    try:
        # Существующая статистика
        faq_ratings = session.query(
            func.sum(ResponseRating.rating).label("positive"),
            func.count(ResponseRating.id).label("total")
        ).filter(ResponseRating.source == "faq").first()

        ai_ratings = session.query(
            func.sum(ResponseRating.rating).label("positive"),
            func.count(ResponseRating.id).label("total")
        ).filter(ResponseRating.source == "ai").first()

        human_ratings = session.query(
            func.sum(ResponseRating.rating).label("positive"),
            func.count(ResponseRating.id).label("total")
        ).filter(ResponseRating.source == "human").first()

        # Вычисляем проценты
        faq_positive_percent = (faq_ratings.positive / faq_ratings.total * 100) if faq_ratings.total > 0 else 0
        ai_positive_percent = (ai_ratings.positive / ai_ratings.total * 100) if ai_ratings.total > 0 else 0
        human_positive_percent = (human_ratings.positive / human_ratings.total * 100) if human_ratings.total > 0 else 0

        # Получаем статистику за последние 7 дней
        seven_days_ago = datetime.datetime.now().date() - datetime.timedelta(days=7)

        # Положительные оценки по дням
        weekly_positive = session.query(
            func.date(ResponseRating.timestamp).label('date'),
            func.count(ResponseRating.id).label('count')
        ).filter(
            func.date(ResponseRating.timestamp) >= seven_days_ago,
            ResponseRating.rating == 1
        ).group_by(func.date(ResponseRating.timestamp)).all()

        # Отрицательные оценки по дням
        weekly_negative = session.query(
            func.date(ResponseRating.timestamp).label('date'),
            func.count(ResponseRating.id).label('count')
        ).filter(
            func.date(ResponseRating.timestamp) >= seven_days_ago,
            ResponseRating.rating == 0
        ).group_by(func.date(ResponseRating.timestamp)).all()

        # Преобразуем в списки для графика
        today = datetime.datetime.now().date()
        weekly_positive_ratings = [0] * 7
        weekly_negative_ratings = [0] * 7

        for date, count in weekly_positive:
            if isinstance(date, str):
                date = datetime.datetime.strptime(date, '%Y-%m-%d').date()
            days_ago = (today - date).days
            if 0 <= days_ago < 7:
                weekly_positive_ratings[days_ago] = count

        for date, count in weekly_negative:
            if isinstance(date, str):
                date = datetime.datetime.strptime(date, '%Y-%m-%d').date()
            days_ago = (today - date).days
            if 0 <= days_ago < 7:
                weekly_negative_ratings[days_ago] = count

        # Разворачиваем списки, чтобы самые старые данные были слева
        weekly_positive_ratings.reverse()
        weekly_negative_ratings.reverse()

        # Получаем последние 10 оценок
        recent_ratings = session.query(ResponseRating).order_by(
            ResponseRating.timestamp.desc()
        ).limit(10).all()

        return render_template(
            "analytics.html",
            faq_ratings=faq_ratings,
            ai_ratings=ai_ratings,
            human_ratings=human_ratings,
            faq_positive_percent=faq_positive_percent,
            ai_positive_percent=ai_positive_percent,
            human_positive_percent=human_positive_percent,
            recent_ratings=recent_ratings,
            weekly_positive_ratings=weekly_positive_ratings,
            weekly_negative_ratings=weekly_negative_ratings
        )
    finally:
        session.close()


@app.route("/analytics_json")
@login_required
@requires_role("admin", "superadmin")
def analytics_json():
    session = SessionLocal()
    try:
        # Получаем последние 10 оценок
        recent_ratings = session.query(ResponseRating).order_by(
            ResponseRating.timestamp.desc()
        ).limit(10).all()

        # Преобразуем данные в формат JSON
        ratings_data = []
        for rating in recent_ratings:
            # Конвертируем время в московское
            moscow_time = to_moscow_time(rating.timestamp)
            ratings_data.append({
                "id": rating.id,
                "rating": rating.rating,
                "source": rating.source,
                "timestamp": moscow_time.strftime("%Y-%m-%d %H:%M:%S")
            })

        return jsonify({
            "ratings": ratings_data
        })
    finally:
        session.close()


@app.route("/users/add", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def add_user():
    username = request.form.get("username")
    password = request.form.get("password")
    role = request.form.get("role")

    if not username or not password or not role:
        flash("Все поля обязательны для заполнения.", "danger")
        return redirect(url_for("manage_users"))

    session = SessionLocal()
    try:
        # Проверяем, существует ли пользователь с таким именем
        existing_user = session.query(User).filter(User.username == username).first()
        if existing_user:
            flash(f"Пользователь с именем {username} уже существует.", "danger")
            return redirect(url_for("manage_users"))

        # Создаем нового пользователя
        hashed_password = generate_password_hash(password)
        new_user = User(
            username=username,
            password=hashed_password,
            role=role,
            _is_active=1  # Устанавливаем активность по умолчанию
        )
        session.add(new_user)
        session.commit()

        flash(f"Пользователь {username} успешно создан.", "success")
        log_action(
            f"Создан пользователь '{username}' с ролью '{role}'",
            action_type='user_create'
        )
    except Exception as e:
        session.rollback()
        flash(f"Ошибка при создании пользователя: {str(e)}", "danger")
    finally:
        session.close()

    return redirect(url_for("manage_users"))


@app.route("/users/edit/<int:user_id>", methods=["POST"])
@login_required
@requires_role("admin", "superadmin")
def edit_user(user_id):
    username = request.form.get("username")
    password = request.form.get("password")
    role = request.form.get("role")

    if not username or not role:
        flash("Имя пользователя и роль обязательны для заполнения.", "danger")
        return redirect(url_for("manage_users"))

    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            flash("Пользователь не найден.", "danger")
            return redirect(url_for("manage_users"))

        # Проверки для суперадмина
        if current_user.role == "superadmin":
            # Суперадмин может делать всё
            pass
        else:
            # Админ не может изменять суперадмина
            if user.role == "superadmin":
                flash("Вы не можете изменять суперадмина.", "danger")
                return redirect(url_for("manage_users"))

            # Админ не может понизить роль другого админа
            if user.role == "admin" and role != "admin" and user.id != current_user.id:
                flash("Вы не можете понизить роль другого администратора.", "danger")
                return redirect(url_for("manage_users"))

            # Админ не может повысить кого-то до суперадмина
            if role == "superadmin":
                flash("Вы не можете назначить роль суперадмина.", "danger")
                return redirect(url_for("manage_users"))

        # Проверяем, не существует ли другой пользователь с таким именем
        existing_user = session.query(User).filter(
            User.username == username,
            User.id != user_id
        ).first()
        if existing_user:
            flash(f"Пользователь с именем {username} уже существует.", "danger")
            return redirect(url_for("manage_users"))

        # Обновляем данные пользователя
        old_username = user.username
        old_role = user.role

        user.username = username
        user.role = role

        # Обновляем пароль только если он был предоставлен
        if password and password.strip():  # проверяем, что пароль не пустой
            user.password = generate_password_hash(password)

        session.commit()

        changes = []
        if old_username != username:
            changes.append(f"имя пользователя с '{old_username}' на '{username}'")
        if old_role != role:
            changes.append(f"роль с '{old_role}' на '{role}'")
        if password and password.strip():  # проверяем, что пароль не пустой
            changes.append("пароль был изменен")

        if changes:  # логируем только если были изменения
            log_action(
                f"Пользователь '{old_username}': " + ", ".join(changes),
                action_type='user_edit'
            )
            flash(f"Пользователь {username} успешно обновлен.", "success")
        else:
            flash("Никаких изменений не было сделано.", "info")
    except Exception as e:
        session.rollback()
        flash(f"Ошибка при редактировании пользователя: {str(e)}", "danger")
    finally:
        session.close()

    return redirect(url_for("manage_users"))


def log_action(action, action_type=None):
    """
    Логирование действий пользователя.
    :param action: Описание действия
    :param action_type: Тип действия (например, "create_user", "delete_user")
    """
    session = SessionLocal()
    try:
        log_entry = Log(
            user_id=current_user.id,
            action=action,
            action_type=action_type,
            timestamp=datetime.datetime.now()
        )
        session.add(log_entry)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Error logging action: {str(e)}")
    finally:
        session.close()


if __name__ == "__main__":
    app.run(debug=True)
