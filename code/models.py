from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
import datetime
from database import Base
from flask_login import UserMixin

class Question(Base):
    __tablename__ = 'questions'
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    question = Column(Text, nullable=False)
    response = Column(Text)
    status = Column(String(50), default="в обработке")
    priority = Column(Integer, default=1)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.now)
    user_id = Column(Integer)
    # другие поля...

class Log(Base):
    __tablename__ = 'logs'
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    question_id = Column(Integer, ForeignKey('questions.id'))
    action = Column(Text)
    action_type = Column(String(50))  # edit, archive, status_change и т.д.
    timestamp = Column(DateTime, default=datetime.datetime.now)
    
    # Отношения для удобного доступа
    user = relationship("User", backref="logs")
    question = relationship("Question", backref="logs")

class User(Base, UserMixin):
    __tablename__ = 'users'
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password = Column(String(255), nullable=False)
    role = Column(String(20), default='user')
    is_active = Column(Boolean, default=False)
    
    logs = relationship("Log", back_populates="user")

    def get_id(self):
        return str(self.id) 