"""Adaptador del OpenAI Codex CLI. `codex exec` = modo no-interactivo.
--output-last-message deja el texto final limpio en un archivo (sin la traza).
Edita archivos con --full-auto. Sin --resume en v1: la continuidad la da el disco."""
import os
import tempfile

from runs import emit
from skills import install_agents_md
from cli_base import _headless_prompt, _find_bin, _bin_version


class CodexAdapter:
    key = "codex"; label = "Codex"; bin_names = ["codex"]; supports_resume = False

    def find(self):
        return _find_bin(self.bin_names)

    def version(self, b):
        return _bin_version(b)

    def install_instructions(self, work_dir):
        install_agents_md(work_dir)

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model, resume,
                  effort=None, editor_target=None):
        # editor_target no aplica: los proyectos editor van solo con Claude Code (v1)
        last = os.path.join(tempfile.gettempdir(), f"codex-last-{run['id']}.txt")
        run["_last_file"] = last
        prompt = _headless_prompt(folder, focus_name, message)
        cmd = [b, "exec", "--full-auto", "--skip-git-repo-check", "--output-last-message", last]
        if model:
            cmd += ["-m", model]
        cmd += [prompt]
        return cmd, {}

    def parse_line(self, run, line):
        if not any(e["kind"] in ("tool", "assistant") for e in run["events"]):
            emit(run, "tool", name="working")

    def finalize(self, run):
        txt = ""
        f = run.get("_last_file")
        if f and os.path.exists(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    txt = fh.read().strip()
                os.remove(f)
            except Exception:
                pass
        if txt:
            emit(run, "assistant", text=txt)
        elif not any(e["kind"] == "assistant" for e in run["events"]):
            emit(run, "assistant", text="(Codex terminó.)")
