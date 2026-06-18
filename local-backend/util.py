"""Utilidades chicas compartidas por el backend local."""


def safe_name(name):
    # nombre de carpeta → dir seguro (legible). Permite espacios.
    s = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
    return s or "default"
