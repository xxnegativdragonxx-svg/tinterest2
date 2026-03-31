from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import List, Optional
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import JWTError, jwt
import os
import json
import uuid
import shutil
import uvicorn

# ===== НАСТРОЙКИ =====
SECRET_KEY = os.getenv("TINTEREST_SECRET_KEY", "dev-secret-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Создать папку для загрузок
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ===== БАЗА ДАННЫХ =====
DATABASE_URL = "sqlite:///./tinterest.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ===== МОДЕЛИ БД =====
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    city = Column(String, default="")
    interests = Column(String, default="[]")  # JSON array string
    department = Column(String, default="Не указан")
    avatar_url = Column(String, default=None, nullable=True)

    messages_sent = relationship("Message", foreign_keys="Message.sender_id", back_populates="sender")
    messages_received = relationship("Message", foreign_keys="Message.receiver_id", back_populates="receiver")
    group_memberships = relationship("GroupMember", back_populates="user")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))
    text = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    sender = relationship("User", foreign_keys=[sender_id], back_populates="messages_sent")
    receiver = relationship("User", foreign_keys=[receiver_id], back_populates="messages_received")

# ===== ГРУППЫ =====
class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    city = Column(String, default="")
    interests = Column(String, default="[]")  # JSON array string
    created_by_user_id = Column(Integer, ForeignKey("users.id"))

    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")
    messages = relationship("GroupMessage", back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    joined_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)

    group = relationship("Group", back_populates="members")
    user = relationship("User", back_populates="group_memberships")


class GroupMessage(Base):
    __tablename__ = "group_messages"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    sender_id = Column(Integer, ForeignKey("users.id"))
    text = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

    group = relationship("Group", back_populates="messages")
    sender = relationship("User", foreign_keys=[sender_id])


# ===== ЛАЙКИ =====
class Like(Base):
    __tablename__ = "likes"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_like = Column(Integer, default=1)  # 1 = лайк, 0 = дизлайк
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("sender_id", "receiver_id", name="uq_like_pair"),)

    sender = relationship("User", foreign_keys=[sender_id], lazy="joined")
    receiver = relationship("User", foreign_keys=[receiver_id], lazy="joined")


# Создать таблицы
Base.metadata.create_all(bind=engine)

# ===== PYDANTIC МОДЕЛИ =====
class UserRegister(BaseModel):
    username: str
    password: str
    city: str = ""
    interests: List[str] = Field(default_factory=list)
    department: Optional[str] = "Не указан"

class UserLogin(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    city: str
    interests: List[str]
    department: str
    avatar_url: Optional[str] = None

class UserUpdate(BaseModel):
    city: Optional[str] = None
    interests: Optional[List[str]] = None
    department: Optional[str] = None

class MessageCreate(BaseModel):
    text: str

class GroupCreate(BaseModel):
    name: str
    city: Optional[str] = ""
    interests: List[str] = Field(default_factory=list)

class GroupResponse(BaseModel):
    id: int
    name: str
    city: str
    interests: List[str]
    members_count: int

class GroupMessageCreate(BaseModel):
    text: str

class GroupMessageResponse(BaseModel):
    id: int
    group_id: int
    sender_id: int
    sender_username: str
    text: str
    timestamp: str

class LikeCreate(BaseModel):
    is_like: bool = True

class LikeResponse(BaseModel):
    id: int
    sender_id: int
    receiver_id: int
    is_like: bool
    created_at: str

class MatchResponse(BaseModel):
    id: int
    user_id: int
    user_username: str
    user_city: str
    user_department: str
    user_interests: List[str]
    user_avatar_url: Optional[str] = None
    liked_at: str

# ===== ПРИЛОЖЕНИЕ =====
app = FastAPI(title="Tinterest API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== БЕЗОПАСНОСТЬ =====
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], default="pbkdf2_sha256")
security = HTTPBearer()

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def _parse_json_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
        return []
    except Exception:
        # Backward compatibility for old comma-separated data
        return [x.strip() for x in value.split(",") if x.strip()]

def _dump_json_list(values: List[str]) -> str:
    cleaned = []
    seen = set()
    for v in values:
        s = str(v).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    return json.dumps(cleaned, ensure_ascii=False)

def _user_to_response(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "city": user.city or "",
        "interests": _parse_json_list(user.interests),
        "department": user.department or "Не указан",
        "avatar_url": user.avatar_url,
    }

def _group_to_response(group: Group, members_count: int) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "city": group.city or "",
        "interests": _parse_json_list(group.interests),
        "members_count": members_count,
    }

# ===== ГОРОДА РФ =====
RUSSIAN_CITIES = [
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань",
    "Нижний Новгород", "Челябинск", "Самара", "Омск", "Ростов-на-Дону",
    "Уфа", "Красноярск", "Воронеж", "Пермь", "Волгоград",
    "Краснодар", "Саратов", "Тюмень", "Тольятти", "Ижевск",
    "Барнаул", "Ульяновск", "Иркутск", "Хабаровск", "Ярославль",
    "Владивосток", "Махачкала", "Томск", "Оренбург", "Кемерово",
    "Новокузнецк", "Рязань", "Астрахань", "Пенза", "Липецк",
    "Киров", "Тула", "Чебоксары", "Калининград", "Брянск",
    "Курск", "Иваново", "Магнитогорск", "Тверь", "Ставрополь",
    "Нижний Тагил", "Белгород", "Сочи", "Архангельск", "Владимир"
]

# ===== ИНТЕРЕСЫ =====
INTERESTS = [
    "Спорт", "Футбол", "Баскетбол", "Теннис", "Плавание", "Бег", "Йога", "Фитнес",
    "Гейминг", "Киберспорт", "Настольные игры",
    "Книги", "Фантастика", "Детективы", "Классика",
    "Кино", "Сериалы", "Аниме", "Документальное",
    "Музыка", "Рок", "Поп", "Классика", "Джаз", "Электронная",
    "Путешествия", "Походы", "Пляжный отдых", "Экстрим",
    "Кулинария", "Выпечка", "Вегетарианство", "Кофе",
    "Технологии", "Программирование", "AI", "Гаджеты",
    "Искусство", "Фотография", "Дизайн", "Рисование",
    "Обучение", "Языки", "Наука", "История"
]

# ===== ЭНДПОИНТЫ =====
@app.post("/api/register")
def register(user: UserRegister, db: Session = Depends(get_db)):
    # Проверка пользователя
    existing = db.query(User).filter(User.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username уже занят")
    
    # Создание пользователя
    db_user = User(
        username=user.username,
        hashed_password=get_password_hash(user.password),
        city=user.city,
        interests=_dump_json_list(user.interests),
        department=user.department
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    # Токен
    access_token = create_access_token(data={"sub": db_user.username})
    return {"access_token": access_token, "user_id": db_user.id, "username": db_user.username}

@app.post("/api/login")
def login(user: UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == user.username).first()
    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    
    access_token = create_access_token(data={"sub": db_user.username})
    return {"access_token": access_token, "user_id": db_user.id, "username": db_user.username}

@app.get("/api/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return _user_to_response(current_user)

@app.get("/api/onboarding/status")
def onboarding_status(current_user: User = Depends(get_current_user)):
    interests = _parse_json_list(current_user.interests)
    completed = bool((current_user.city or "").strip()) and len(interests) > 0
    return {"completed": completed}

@app.put("/api/me/profile", response_model=UserResponse)
def update_profile(payload: UserUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if payload.city is not None:
        current_user.city = payload.city
    if payload.department is not None:
        current_user.department = payload.department
    if payload.interests is not None:
        current_user.interests = _dump_json_list(payload.interests)
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return _user_to_response(current_user)

@app.get("/api/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_to_response(user)

@app.get("/api/users/search")
def search_users(query: str, limit: int = 20, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Поиск пользователей по username, city, department, interests"""
    if not query.strip():
        return []
    
    search_term = f"%{query.strip().lower()}%"
    
    # Поиск по username
    users = db.query(User).filter(
        User.username.ilike(search_term)
    ).filter(User.id != current_user.id).limit(limit).all()
    
    # Если найдено мало, ищем по городу
    if len(users) < limit:
        city_users = db.query(User).filter(
            User.city.ilike(search_term)
        ).filter(User.id != current_user.id).limit(limit - len(users)).all()
        for u in city_users:
            if u not in users:
                users.append(u)
    
    # Если всё ещё мало, ищем по отделу
    if len(users) < limit:
        dept_users = db.query(User).filter(
            User.department.ilike(search_term)
        ).filter(User.id != current_user.id).limit(limit - len(users)).all()
        for u in dept_users:
            if u not in users:
                users.append(u)
    
    return [_user_to_response(u) for u in users[:limit]]

@app.get("/api/matches/{user_id}")
def get_matches(user_id: int, limit: int = 10, offset: int = 0, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    current_user = db.query(User).filter(User.id == user_id).first()
    if not current_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    current_interests = set(_parse_json_list(current_user.interests))
    
    matches = []
    for candidate in db.query(User).filter(User.id != user_id).all():
        score = 0
        reasons = []

        # Локация — даем сильный бонус, если совпадает
        if candidate.city == current_user.city and (candidate.city or "").strip():
            score += 50
            reasons.append("город совпадает")

        # Общие интересы
        candidate_interests = set(_parse_json_list(candidate.interests))
        common = current_interests & candidate_interests
        common_list = sorted(common)
        if len(common) >= 2:
            score += 30
            reasons.append(f"{len(common)} общих интереса")
        elif len(common) == 1:
            score += 15
            reasons.append("1 общий интерес")

        # Отдел — мягкий бонус только при совпадении
        if (candidate.department or "").strip() and candidate.department == current_user.department:
            score += 20
            reasons.append("отдел совпадает")

        if score > 0:
            matches.append({
                "user": _user_to_response(candidate),
                "score": score,
                "reasons": reasons,
                "common_interests": common_list,
            })

    matches.sort(key=lambda x: x["score"], reverse=True)
    
    # Пагинация
    total = len(matches)
    start = offset
    end = offset + limit
    paginated_matches = matches[start:end]
    
    return {
        "matches": paginated_matches,
        "total": total,
        "has_more": end < total
    }

@app.get("/api/chat/{user_id}/{other_id}")
def get_messages(user_id: int, other_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    chat_messages = db.query(Message).filter(
        ((Message.sender_id == user_id) & (Message.receiver_id == other_id)) |
        ((Message.sender_id == other_id) & (Message.receiver_id == user_id))
    ).order_by(Message.timestamp).all()
    
    return [
        {
            "id": msg.id,
            "sender_id": msg.sender_id,
            "sender_username": msg.sender.username if msg.sender else "",
            "text": msg.text,
            "timestamp": msg.timestamp.isoformat()
        }
        for msg in chat_messages
    ]

@app.post("/api/chat/{user_id}/{other_id}")
def send_message(user_id: int, other_id: int, message: MessageCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    db_msg = Message(
        sender_id=user_id,
        receiver_id=other_id,
        text=message.text
    )
    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)
    
    return {
        "id": db_msg.id,
        "sender_id": db_msg.sender_id,
        "sender_username": current_user.username,
        "text": db_msg.text,
        "timestamp": db_msg.timestamp.isoformat()
    }

@app.get("/api/cities")
def get_cities(query: Optional[str] = None):
    if query:
        return [c for c in RUSSIAN_CITIES if query.lower() in c.lower()][:10]
    return RUSSIAN_CITIES[:20]

@app.get("/api/interests")
def get_interests(query: Optional[str] = None):
    if query:
        return [i for i in INTERESTS if query.lower() in i.lower()][:15]
    return INTERESTS[:20]

@app.post("/api/groups", response_model=GroupResponse)
def create_group(payload: GroupCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Group name is required")
    group = Group(
        name=payload.name.strip(),
        city=(payload.city or "").strip(),
        interests=_dump_json_list(payload.interests),
        created_by_user_id=current_user.id,
    )
    db.add(group)
    db.commit()
    db.refresh(group)

    membership = GroupMember(group_id=group.id, user_id=current_user.id)
    db.add(membership)
    db.commit()

    return _group_to_response(group, members_count=1)

@app.get("/api/groups/my", response_model=List[GroupResponse])
def my_groups(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(Group, GroupMember)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == current_user.id)
        .order_by(Group.created_at.desc())
        .all()
    )
    groups = []
    for g, _gm in rows:
        members_count = db.query(GroupMember).filter(GroupMember.group_id == g.id).count()
        groups.append(_group_to_response(g, members_count))
    return groups

@app.get("/api/groups/recommended", response_model=List[GroupResponse])
def recommended_groups(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    user_interests = set(_parse_json_list(current_user.interests))
    user_city = (current_user.city or "").strip()

    results = []
    for g in db.query(Group).all():
        g_interests = set(_parse_json_list(g.interests))
        common = len(user_interests & g_interests) if user_interests else 0
        score = common * 10
        if user_city and g.city and g.city == user_city:
            score += 15
        members_count = db.query(GroupMember).filter(GroupMember.group_id == g.id).count()
        if score > 0 or members_count > 0:
            results.append((score, g, members_count))

    results.sort(key=lambda x: (x[0], x[2], x[1].created_at), reverse=True)
    return [_group_to_response(g, members_count) for score, g, members_count in results[:20]]

@app.post("/api/groups/{group_id}/join", response_model=GroupResponse)
def join_group(group_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    existing = (
        db.query(GroupMember)
        .filter(GroupMember.group_id == group_id, GroupMember.user_id == current_user.id)
        .first()
    )
    if not existing:
        db.add(GroupMember(group_id=group_id, user_id=current_user.id))
        db.commit()
    members_count = db.query(GroupMember).filter(GroupMember.group_id == group_id).count()
    return _group_to_response(group, members_count)

@app.get("/api/groups/{group_id}/messages", response_model=List[GroupMessageResponse])
def get_group_messages(group_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    is_member = (
        db.query(GroupMember)
        .filter(GroupMember.group_id == group_id, GroupMember.user_id == current_user.id)
        .first()
        is not None
    )
    if not is_member:
        raise HTTPException(status_code=403, detail="Join group first")
    msgs = (
        db.query(GroupMessage)
        .filter(GroupMessage.group_id == group_id)
        .order_by(GroupMessage.timestamp)
        .all()
    )
    return [
        {
            "id": m.id,
            "group_id": m.group_id,
            "sender_id": m.sender_id,
            "sender_username": m.sender.username if m.sender else "",
            "text": m.text,
            "timestamp": m.timestamp.isoformat(),
        }
        for m in msgs
    ]

@app.post("/api/groups/{group_id}/messages", response_model=GroupMessageResponse)
def send_group_message(group_id: int, payload: GroupMessageCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    is_member = (
        db.query(GroupMember)
        .filter(GroupMember.group_id == group_id, GroupMember.user_id == current_user.id)
        .first()
        is not None
    )
    if not is_member:
        raise HTTPException(status_code=403, detail="Join group first")
    msg = GroupMessage(group_id=group_id, sender_id=current_user.id, text=payload.text)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {
        "id": msg.id,
        "group_id": msg.group_id,
        "sender_id": msg.sender_id,
        "sender_username": current_user.username,
        "text": msg.text,
        "timestamp": msg.timestamp.isoformat(),
    }

# ===== ЛАЙКИ =====
@app.post("/api/likes/{user_id}", response_model=LikeResponse)
def send_like(user_id: int, payload: LikeCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Нельзя лайкнуть себя")
    
    # Проверка, существует ли пользователь
    receiver = db.query(User).filter(User.id == user_id).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    # Проверка, есть ли уже лайк
    existing = db.query(Like).filter(
        Like.sender_id == current_user.id,
        Like.receiver_id == user_id
    ).first()
    
    if existing:
        # Обновляем существующий лайк
        existing.is_like = 1 if payload.is_like else 0
        db.commit()
        db.refresh(existing)
        return {
            "id": existing.id,
            "sender_id": existing.sender_id,
            "receiver_id": existing.receiver_id,
            "is_like": bool(existing.is_like),
            "created_at": existing.created_at.isoformat()
        }
    
    # Создаём новый лайк
    like = Like(
        sender_id=current_user.id,
        receiver_id=user_id,
        is_like=1 if payload.is_like else 0
    )
    db.add(like)
    db.commit()
    db.refresh(like)
    
    return {
        "id": like.id,
        "sender_id": like.sender_id,
        "receiver_id": like.receiver_id,
        "is_like": bool(like.is_like),
        "created_at": like.created_at.isoformat()
    }

@app.get("/api/likes/received", response_model=List[MatchResponse])
def get_received_likes(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Получить входящие лайки (кто лайкнул меня)"""
    likes = db.query(Like).filter(
        Like.receiver_id == current_user.id,
        Like.is_like == 1
    ).order_by(Like.created_at.desc()).all()
    
    result = []
    for like in likes:
        result.append({
            "id": like.id,
            "user_id": like.sender_id,
            "user_username": like.sender.username,
            "user_city": like.sender.city or "",
            "user_department": like.sender.department or "Не указан",
            "user_interests": _parse_json_list(like.sender.interests),
            "user_avatar_url": None,
            "liked_at": like.created_at.isoformat()
        })
    
    return result

@app.get("/api/matches", response_model=List[MatchResponse])
def get_matches_list(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Получить взаимные матчи (когда лайкнули друг друга)"""
    # Находим всех, кого лайкнул текущий пользователь
    my_likes = db.query(Like.receiver_id).filter(
        Like.sender_id == current_user.id,
        Like.is_like == 1
    ).subquery()
    
    # Находим взаимные лайки
    matches = db.query(Like).filter(
        Like.sender_id.in_(my_likes),
        Like.receiver_id == current_user.id,
        Like.is_like == 1
    ).all()
    
    result = []
    for like in matches:
        liker = like.sender
        result.append({
            "id": like.id,
            "user_id": liker.id,
            "user_username": liker.username,
            "user_city": liker.city or "",
            "user_department": liker.department or "Не указан",
            "user_interests": _parse_json_list(liker.interests),
            "user_avatar_url": None,
            "liked_at": like.created_at.isoformat()
        })
    
    return result

# ===== АВАТАРКИ =====
def _get_avatar_url(filename: str) -> str:
    return f"{BACKEND_URL}/api/avatars/{filename}"

@app.post("/api/me/avatar")
def upload_avatar(file: UploadFile = File(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Загрузить аватарку пользователя"""
    # Проверка расширения
    ext = file.filename.split(".")[-1].lower() if file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Разрешены только: {', '.join(ALLOWED_EXTENSIONS)}")
    
    # Генерация уникального имени
    unique_filename = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)
    
    # Чтение и проверка размера
    contents = b""
    while chunk := file.file.read(8192):
        contents += chunk
        if len(contents) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="Файл слишком большой (макс. 5MB)")
    
    # Сохранение
    with open(file_path, "wb") as f:
        f.write(contents)
    
    # Обновление пользователя
    avatar_url = _get_avatar_url(unique_filename)
    current_user.avatar_url = avatar_url
    db.commit()
    
    return {"avatar_url": avatar_url}

@app.get("/api/avatars/{filename}")
def get_avatar(filename: str):
    """Получить аватарку по имени файла"""
    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Аватарка не найдена")
    
    from fastapi.responses import FileResponse
    return FileResponse(file_path, media_type="image/jpeg")

@app.delete("/api/me/avatar")
def delete_avatar(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Удалить аватарку пользователя"""
    if current_user.avatar_url:
        # Извлечь имя файла из URL
        filename = current_user.avatar_url.split("/")[-1]
        file_path = os.path.join(UPLOAD_DIR, filename)
        
        # Удалить файл
        if os.path.exists(file_path):
            os.remove(file_path)
        
        # Обновить пользователя
        current_user.avatar_url = None
        db.commit()
    
    return {"message": "Аватарка удалена"}

@app.get("/")
def read_root():
    return {"message": "Tinterest API работает! 🧡"}
