from flask import Blueprint, render_template, redirect, request, url_for, flash, session
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from database import SessionLocal, User
from utils import log_action

auth = Blueprint('auth', __name__)

# Flask-Login настройка
login_manager = LoginManager()
login_manager.login_view = 'auth.login'


@login_manager.user_loader
def load_user(user_id):
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        return user
    finally:
        session.close()


# Регистрация
@auth.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = generate_password_hash(request.form["password"])

        session = SessionLocal()
        try:
            # Проверяем, существует ли пользователь с таким именем
            existing_user = session.query(User).filter(User.username == username).first()
            if existing_user:
                flash("Имя пользователя уже существует.", "danger")
                return render_template("register.html")

            # Создаем нового пользователя
            new_user = User(
                username=username,
                password=password,
                role="user",
                _is_active=0  # Используем _is_active вместо is_active
            )
            session.add(new_user)
            session.commit()

            # Логируем регистрацию
            log_action(new_user.id, f"Зарегестрирован новый пользователь '{username}'", "user_register")

            flash("Регистрация успешна! Ожидайте подтверждения администратора.", "success")
            return redirect(url_for("auth.login"))
        except Exception as e:
            session.rollback()
            flash(f"Ошибка при регистрации: {str(e)}", "danger")
        finally:
            session.close()

    return render_template("register.html")


@auth.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get('username')
        password = request.form.get('password')

        session = SessionLocal()
        try:
            user = session.query(User).filter_by(username=username).first()

            if user and check_password_hash(user.password, password):
                # Проверяем, активен ли пользователь
                if user.is_active or user.role in ["admin", "superadmin"]:
                    login_user(user)
                    # Добавляем лог успешного входа
                    log_action(
                        user_id=user.id,
                        action=f"Пользователь {username} вошел в систему",
                        action_type='login'
                    )
                    return redirect(url_for('index'))
                else:
                    flash('Ваша учетная запись не активирована.', 'danger')
            else:
                flash('Неверное имя пользователя или пароль.', 'danger')
        finally:
            session.close()

    return render_template('login.html')


# Логаут
@auth.route("/logout")
@login_required
def logout():
    # Добавляем лог выхода перед самим выходом
    log_action(
        user_id=current_user.id,
        action=f"Пользователь {current_user.username} вышел из системы",
        action_type='logout'
    )
    logout_user()
    return redirect(url_for('auth.login'))