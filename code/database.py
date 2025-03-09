from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from flask_login import UserMixin
from werkzeug.security import generate_password_hash
import datetime
import os

DB_PATH = "support_bot.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# Создаём движок подключения к базе данных. Параметр check_same_thread=False необходим для SQLite.
engine = create_engine(DATABASE_URL, echo=True, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Модель таблицы вопросов
class Question(Base):
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    question = Column(Text, nullable=False)
    status = Column(String, default="в обработке")
    response = Column(Text, nullable=True)
    is_archived = Column(Integer, default=0)
    priority = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.now)
    logs = relationship("Log", back_populates="question")
    ratings = relationship("ResponseRating", back_populates="question")

# Модель таблицы пользователей
class User(Base, UserMixin):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    role = Column(String, default="user")
    _is_active = Column("is_active", Integer, default=1)
    logs = relationship("Log", back_populates="user")

# Модель таблицы логов
class Log(Base):
    __tablename__ = "logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    question_id = Column(Integer, ForeignKey('questions.id'))
    action = Column(String, nullable=False)
    action_type = Column(String(50), nullable=True)
    timestamp = Column(DateTime, default=func.now(), nullable=False)
    details = Column(Text, nullable=True)
    user = relationship("User", back_populates="logs")
    question = relationship("Question", back_populates="logs")

# Модель истории изменений ответа
class AnswerHistory(Base):
    __tablename__ = "answer_history"
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey('questions.id'), nullable=False)
    editor_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    response = Column(Text, nullable=False)
    details = Column(Text)
    timestamp = Column(DateTime, default=func.now())

# Модель FAQ
class FAQ(Base):
    __tablename__ = "faq"
    id = Column(Integer, primary_key=True, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)

# Добавьте новую модель для оценок ответов
class ResponseRating(Base):
    __tablename__ = "response_ratings"
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=True)
    user_id = Column(Integer, nullable=False)
    rating = Column(Integer, nullable=False)  # 1 - положительная, 0 - отрицательная
    source = Column(String, nullable=False)  # 'faq', 'ai', 'human'
    timestamp = Column(DateTime, default=func.now())
    
    question = relationship("Question", back_populates="ratings")

def init_db():
    Base.metadata.create_all(bind=engine)
    print("База данных инициализирована с использованием SQLAlchemy.")

if __name__ == "__main__":
    init_db()

    # Пример наполнения базы данными для таблицы FAQ
    session = SessionLocal()
    faq_entries = [
        {"question": "Как сменить пароль?", "answer": "Вы можете сменить пароль в настройках безопасности."},
        {"question": "Какие у вас часы работы?", "answer": "Мы работаем с 9:00 до 18:00 по МСК."},
        {"question": "Как связаться с поддержкой?", "answer": "Вы можете написать в наш Telegram-бот или отправить email на support@example.com."}
    ]
    for entry in faq_entries:
        # Перед добавлением можно проверить, нет ли похожей записи
        faq = FAQ(**entry)
        session.add(faq)
    session.commit()
    session.close()

# Функция для создания таблиц и суперадмина
def create_tables_and_superadmin():
    Base.metadata.create_all(bind=engine)
    
    # Проверяем, существует ли уже суперадмин
    session = SessionLocal()
    try:
        superadmin = session.query(User).filter(User.username == "admin").first()
        if not superadmin:
            # Создаем суперадмина с логином/паролем admin/admin
            hashed_password = generate_password_hash("admin")
            superadmin = User(
                username="admin",
                password=hashed_password,
                role="superadmin",
                _is_active=1
            )
            session.add(superadmin)
            session.commit()
            print("Суперадмин создан с логином 'admin' и паролем 'admin'")
        else:
            print("Суперадмин уже существует")
    except Exception as e:
        session.rollback()
        print(f"Ошибка при создании суперадмина: {e}")
    finally:
        session.close()

# Вызываем функцию для создания таблиц и суперадмина
create_tables_and_superadmin()

# Переопределяем метод is_active для Flask-Login
@property
def is_active(self):
    return bool(self._is_active)

@is_active.setter
def is_active(self, value):
    self._is_active = int(value)
