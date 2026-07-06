"""Adaptador del Google Gemini CLI. `-p` = prompt no-interactivo; `--yolo` auto-aprueba
las tools (incluida la escritura de archivos). Imprime la respuesta final a stdout.
Sin --resume en v1: la continuidad la da el estado en disco (el tree.json).

Gemini CLI tiene "trusted folders": en headless, si la carpeta no está confiada,
IGNORA el --yolo y no edita. Se confía con la env var GEMINI_CLI_TRUST_WORKSPACE=true."""
import re

from runs import emit, set_status
from skills import install_agents_md
from cli_base import _headless_prompt, _find_bin, _bin_version

_ANSI = re.compile(r"\x1b\[[0-9;]*m")        # códigos de color de la terminal
_NOISE = ("YOLO mode is enabled", "Approval mode overridden",
          "not running in a trusted", "trust this directory")
# señales de que se acabó la cuota / rate limit (free tier de Google es bajísimo)
_QUOTA = ("exceeded your current quota", "exhausted your daily quota",
          "RESOURCE_EXHAUSTED", "TerminalQuotaError", "rate-limit")


class GeminiAdapter:
    key = "gemini"; label = "Gemini CLI"; bin_names = ["gemini"]; supports_resume = False

    def find(self):
        return _find_bin(self.bin_names)

    def version(self, b):
        return _bin_version(b)

    def install_instructions(self, work_dir):
        install_agents_md(work_dir)

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model, resume,
                  effort=None, editor_target=None, editor_relay=None):
        # editor_target/relay no aplican: los proyectos editor van solo con Claude Code (v1)
        prompt = _headless_prompt(folder, focus_name, message)
        cmd = [b, "--yolo"]
        if model:
            cmd += ["-m", model]          # antes del -p (el orden a veces importa)
        cmd += ["-p", prompt]
        # confiar la carpeta para que --yolo valga en modo headless
        return cmd, {"GEMINI_CLI_TRUST_WORKSPACE": "true"}

    def parse_line(self, run, line):
        clean = _ANSI.sub("", line)
        if any(q in clean for q in _QUOTA):
            run["_quota"] = True
        clean = clean.strip()
        if clean and not any(n in clean for n in _NOISE):
            run.setdefault("_buf", []).append(clean)
        if not any(e["kind"] == "tool" for e in run["events"]):
            emit(run, "tool", name="working")

    def finalize(self, run):
        # cuota agotada → mensaje limpio + cerramos OK (sin volcar el stack trace)
        if run.get("_quota"):
            emit(run, "assistant", text="⚠ Gemini CLI se quedó sin cuota (límite del free "
                 "tier de Google, se resetea por día). Cambiá de backend (ej. Local · Claude "
                 "Code) para seguir, o usá una API key de Gemini con plan pago.")
            set_status(run, "done")
            return
        txt = "\n".join(run.get("_buf", [])).strip()
        if txt:
            emit(run, "assistant", text=txt)
        elif not any(e["kind"] == "assistant" for e in run["events"]):
            emit(run, "assistant", text="(Gemini terminó.)")
