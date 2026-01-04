from passlib.context import CryptContext

# Настройка bcrypt
# нужно, чтобы работало без ошибок при длинных паролях за счёт обрезки вручную)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    # здесь я обрезал пароль до 72 символов перед хэшированием
    return pwd_context.hash(password[:72])

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # до 72 символов для проверки
    return pwd_context.verify(plain_password[:72], hashed_password)