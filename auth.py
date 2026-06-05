"""Auth — DISABLED (Databricks workspace SSO handles authentication)."""

def get_current_user(credentials=None):
    return {"sub": "databricks-user", "name": "User", "role": "admin"}

def get_current_principal(credentials=None):
    return {"sub": "databricks-user", "role": "admin"}

def hash_password(password):
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()
