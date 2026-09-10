from config import OWNER_ID


def is_owner(user_id: int | None) -> bool:
    return bool(user_id and OWNER_ID and user_id == OWNER_ID)


def require_owner(user_id: int | None):
    if not is_owner(user_id):
        raise PermissionError("Owner-only command.")
