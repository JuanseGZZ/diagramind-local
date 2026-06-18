"""Adaptador del Google Gemini CLI. `-p` = prompt no-interactivo; `--yolo` auto-aprueba
las tools (incluida la escritura de archivos). Imprime la respuesta final a stdout.
Sin --resume en v1: la continuidad la da el estado en disco (el tree.json)."""
from runs import emit
from skills import install_agents_md
from cli_base import _focus_note, _find_bin, _bin_version


class GeminiAdapter:
    key = "gemini"; label = "Gemini CLI"; bin_names = ["gemini"]; supports_resume = False

    def find(self):
        return _find_bin(self.bin_names)

    def version(self, b):
        return _bin_version(b)

    def install_instructions(self, work_dir):
        install_agents_md(work_dir)

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model, resume):
        prompt = _focus_note(folder, focus_name) + "\n\n" + message
        cmd = [b, "--yolo", "-p", prompt]
        if model:
            cmd += ["-m", model]
        return cmd, {}

    def parse_line(self, run, line):
        run.setdefault("_buf", []).append(line)
        if not any(e["kind"] == "tool" for e in run["events"]):
            emit(run, "tool", name="working")

    def finalize(self, run):
        txt = "\n".join(run.get("_buf", [])).strip()
        if txt:
            emit(run, "assistant", text=txt)
        elif not any(e["kind"] == "assistant" for e in run["events"]):
            emit(run, "assistant", text="(Gemini terminó.)")
