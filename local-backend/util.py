"""Utilidades chicas compartidas por el backend local."""


def safe_name(name):
    # nombre de carpeta → dir seguro (legible). Permite espacios.
    s = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
    return s or "default"


def safe_file_name(name):
    # nombre de ARCHIVO seguro: a diferencia de safe_name, conserva el punto de la
    # extensión (foto.png). Se queda solo con el último segmento de la ruta (sin
    # separadores) para que un adjunto no pueda escribir fuera del temp.
    base = str(name).replace("\\", "/").split("/")[-1]
    s = "".join(c for c in base if c.isalnum() or c in "-_. ").strip()
    s = s.lstrip(".")          # nada de archivos ocultos / nombres vacíos
    return s or "archivo"
