from database import SessionLocal, Log
from datetime import datetime

def log_action(user_id, action, action_type=None):
    session = SessionLocal()
    try:
        log_entry = Log(
            user_id=user_id,
            action=action,
            action_type=action_type,
            timestamp=datetime.now()
        )
        session.add(log_entry)
        session.commit()
    except Exception as e:
        print(f"Error logging action: {e}")
        session.rollback()
    finally:
        session.close()
