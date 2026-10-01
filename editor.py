#!/usr/bin/env python3
"""Raw-mode line editor with a live suggestion menu, history and pipe fallback.

Public API
----------
``AVAILABLE``
    True when stdin *and* stdout are terminals on a POSIX host (``termios``
    usable).  When it is False the editor cannot own the screen and
    :meth:`Editor.read_line` degrades to the builtin ``input()``.
``Editor``
    ``read_line(prompt) -> str`` reads one line with a suggestion menu drawn
    above the input line, input history, and the usual emacs bindings.

Notes
-----
* The editor redraws its own block (menu rows + input row) on every keystroke;
  the caller must not print to stdout while ``read_line`` is blocked on input,
  because that would move the cursor away from the block the editor repaints.
  Output written between two ``read_line`` calls is fine (proved by the demo,
  which echoes every accepted line).
* ``Ctrl-C`` raises ``KeyboardInterrupt``, ``Ctrl-D`` on an empty line raises
  ``EOFError``; the terminal attributes are always restored in a ``finally``
  block, including when the editor itself fails.
* Menu items accept the token under the cursor (up to the nearest whitespace),
  so typing ``/th`` and pressing Tab with ``("/theme", ...)`` yields ``/theme``
  with the cursor right after it.
* With the menu open, ``Enter`` (and ``Right`` at the end of the line) accepts
  the highlighted entry instead of sending the line, as long as its token
  differs from what is already under the cursor.  The suggestions are then
  re-queried for the new text, so the menu cascades (a completed command, then
  its arguments) and a second ``Enter`` sends the line once the token is an
  exact match or the menu has closed.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unicodedata
from typing import Callable

try:  # POSIX only: Windows has neither module.
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - platform dependent
    select = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

__all__ = ["AVAILABLE", "Editor"]

RESET = "\033[0m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_TO_END = "\033[J"
ERASE_SCREEN = "\033[2J\033[H"
REVERSE = "\033[7m"
NL = "\r\n"
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

MAX_HISTORY = 500
DEFAULT_ROWS = 8
SELECTED_MARKER = "❯ "
PLAIN_MARKER = "  "

# Escape sequences sent by the arrow/home/end/delete keys.
SEQUENCES: dict[str, str] = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end",
    "1~": "home", "2~": "insert", "3~": "delete", "4~": "end",
    "5~": "pageup", "6~": "pagedown", "7~": "home", "8~": "end",
}

# Control bytes -> logical key names.
CONTROLS: dict[int, str] = {
    0x01: "ctrl-a", 0x02: "left", 0x05: "ctrl-e", 0x06: "right",
    0x0B: "ctrl-k", 0x0C: "ctrl-l", 0x0E: "down", 0x10: "up",
    0x15: "ctrl-u", 0x17: "ctrl-w",
}


def _is_tty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


AVAILABLE: bool = bool(
    os.name == "posix"
    and termios is not None
    and tty is not None
    and select is not None
    and _is_tty(sys.stdin)
    and _is_tty(sys.stdout)
)


def _plain(text: str, key: str) -> str:
    """Default paint: no colour codes."""
    return text


def _char_width(char: str) -> int:
    """Terminal columns used by one character (East Asian wide = 2)."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _plain_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _visible_width(text: str) -> int:
    return _plain_width(ANSI_RE.sub("", text))


def _fit(text: str, width: int) -> str:
    """Clip ANSI-painted *text* to *width* visible columns."""
    if width <= 0:
        return ""
    if _visible_width(text) <= width:
        return text
    out: list[str] = []
    shown = 0
    index = 0
    limit = max(1, width - 1)
    while index < len(text):
        match = ANSI_RE.match(text, index)
        if match:
            out.append(match.group())
            index = match.end()
            continue
        step = _char_width(text[index])
        if shown + step > limit:
            out.append("…")
            break
        out.append(text[index])
        shown += step
        index += 1
    out.append(RESET)
    return "".join(out)


class Editor:
    """Line editor with a suggestion palette, history and a safe fallback.

    ``suggestions(text) -> list[(insert_text, description)]`` is called after
    every change of the buffer; a non-empty result opens the menu above the
    input line.  ``paint(text, style_key) -> str`` colourises the menu, with
    ``style_key`` one of ``'dim' | 'accent' | 'ok' | 'error' | 'label'``.
    """

    def __init__(
        self,
        history_path: str | None = None,
        suggestions: Callable[[str], list[tuple[str, str]]] | None = None,
        paint: Callable[[str, str], str] | None = None,
        max_rows: int = DEFAULT_ROWS,
    ) -> None:
        self.history_path = history_path
        self.suggestions = suggestions or (lambda text: [])
        self.paint = paint or _plain
        self.max_rows = max(1, int(max_rows))

        self.history: list[str] = []
        self._load_history()

        self._fd = -1
        self._buffer = ""
        self._pos = 0
        self._sugg: list[tuple[str, str]] = []
        self._sel = 0
        self._dismissed = False
        self._rows = 0
        self._hist_pos: int | None = None
        self._draft = ""

    # ---------------------------------------------------------------- public

    def read_line(self, prompt: str) -> str:
        """Read one line.  Ctrl-C -> KeyboardInterrupt, Ctrl-D (empty) -> EOFError."""
        if not AVAILABLE:
            return input(prompt)
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except (OSError, ValueError, AttributeError, termios.error):
            return input(prompt)

        self._fd = fd
        self._buffer = ""
        self._pos = 0
        self._sel = 0
        self._dismissed = False
        self._rows = 0
        self._hist_pos = None
        self._draft = ""
        self._refresh()

        completed = False
        try:
            try:
                # TCSADRAIN keeps bytes typed before the editor started.
                tty.setraw(fd, termios.TCSADRAIN)
            except TypeError:  # pragma: no cover - Python < 3.12
                tty.setraw(fd)
            sys.stdout.write(HIDE_CURSOR)
            self._render(prompt)
            while True:
                kind, value = self._read_key()
                if kind == "text":
                    self._insert(value)
                elif value == "enter":
                    if not self._accept_pending():
                        completed = True
                        break
                elif value == "ctrl-d":
                    if not self._buffer:
                        raise EOFError("end of input")
                    self._delete()
                elif value == "tab":
                    self._accept()
                elif value == "up":
                    self._navigate(-1)
                elif value == "down":
                    self._navigate(1)
                elif value == "esc":
                    self._dismissed = True
                elif value == "left":
                    self._pos = max(0, self._pos - 1)
                elif value == "right":
                    if not (self._pos >= len(self._buffer) and self._accept_pending()):
                        self._pos = min(len(self._buffer), self._pos + 1)
                elif value in ("home", "ctrl-a"):
                    self._pos = 0
                elif value in ("end", "ctrl-e"):
                    self._pos = len(self._buffer)
                elif value == "backspace":
                    self._backspace()
                elif value == "delete":
                    self._delete()
                elif value == "ctrl-u":
                    self._buffer = self._buffer[self._pos:]
                    self._pos = 0
                    self._on_edit()
                elif value == "ctrl-k":
                    self._buffer = self._buffer[: self._pos]
                    self._on_edit()
                elif value == "ctrl-w":
                    self._kill_word()
                elif value == "ctrl-l":
                    self._rows = 0
                    sys.stdout.write(ERASE_SCREEN)
                self._render(prompt)
        finally:
            try:
                # Still in raw mode: the block's "\r\n" must not be rewritten
                # by the output post-processing of the restored terminal.
                self._teardown(prompt, completed)
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except (OSError, termios.error):  # never mask the original failure
                    pass

        self._remember(self._buffer)
        return self._buffer

    # ------------------------------------------------------------- rendering

    def _render(self, prompt: str) -> None:
        width = shutil.get_terminal_size((80, 24)).columns
        menu = self._menu_lines(width)
        out: list[str] = []
        up = self._rows - 1
        if up > 0:
            out.append(f"\033[{up}A")
        out.append("\r" + CLEAR_TO_END)
        for line in menu:
            out.append(line + NL)
        out.append(self._input_line(prompt, width))
        self._rows = len(menu) + 1
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _input_line(self, prompt: str, width: int) -> str:
        """Prompt plus a windowed view of the buffer, with the cursor placed.

        The window never exceeds the terminal width in *columns*, so the input
        row stays a single physical row and the editor's row accounting (used
        to move back over the block on the next repaint) stays correct.
        """
        prompt_width = _visible_width(prompt)
        available = max(4, width - prompt_width - 1)
        start = self._pos
        used = 0
        while start > 0:
            step = _char_width(self._buffer[start - 1])
            if used + step > available:
                break
            used += step
            start -= 1
        end = start
        used = 0
        while end < len(self._buffer):
            step = _char_width(self._buffer[end])
            if used + step > available:
                break
            used += step
            end += 1
        shown = self._buffer[start:end]
        cursor_column = prompt_width + _plain_width(self._buffer[start : self._pos])
        column = prompt_width + _plain_width(shown)
        line = prompt + shown
        if cursor_column < column:
            line += f"\033[{column - cursor_column}D"
        return line

    def _menu_lines(self, width: int) -> list[str]:
        if not self._menu_open():
            return []
        total = len(self._sugg)
        offset = 0
        rows = self._sugg
        if total > self.max_rows:
            offset = max(0, min(self._sel - self.max_rows // 2, total - self.max_rows))
            rows = self._sugg[offset : offset + self.max_rows]
        lines: list[str] = []
        for index, (text, description) in enumerate(rows):
            selected = (offset + index) == self._sel
            marker = SELECTED_MARKER if selected else PLAIN_MARKER
            painted = self.paint(marker, "label") + self.paint(text, "accent")
            if description:
                painted += "  " + self.paint(description, "dim")
            if selected:
                # Keep the colours alive inside a full-row reverse video block.
                painted = REVERSE + painted.replace(RESET, RESET + REVERSE) + RESET
            lines.append(_fit(painted, width - 1))
        return lines

    def _teardown(self, prompt: str, keep: bool) -> None:
        """Erase the drawn block and leave the cursor at the start of a new line."""
        out = [SHOW_CURSOR]
        if self._rows:
            if self._rows > 1:
                out.append(f"\033[{self._rows - 1}A")
            out.append("\r" + CLEAR_TO_END)
            if keep:
                out.append(prompt + self._buffer)
            out.append(NL)
        elif keep:
            out.append(prompt + self._buffer + NL)
        self._rows = 0
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # ------------------------------------------------------------------ state

    def _menu_open(self) -> bool:
        return bool(self._sugg) and not self._dismissed

    def _refresh(self) -> None:
        text = self._buffer
        try:
            items = list(self.suggestions(text) or [])
        except Exception:  # a broken callback must not kill the editor
            items = []
        cleaned: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, (tuple, list)) or not item:
                continue
            value = str(item[0])
            description = str(item[1]) if len(item) > 1 and item[1] else ""
            cleaned.append((value, description))
        self._sugg = cleaned
        self._sel = 0

    def _on_edit(self, reset_history: bool = True) -> None:
        if reset_history:
            self._hist_pos = None
            self._draft = ""
        self._dismissed = False
        self._refresh()

    # ---------------------------------------------------------------- editing

    def _insert(self, text: str) -> None:
        self._buffer = self._buffer[: self._pos] + text + self._buffer[self._pos :]
        self._pos += len(text)
        self._on_edit()

    def _backspace(self) -> None:
        if self._pos == 0:
            return
        self._buffer = self._buffer[: self._pos - 1] + self._buffer[self._pos :]
        self._pos -= 1
        self._on_edit()

    def _delete(self) -> None:
        if self._pos >= len(self._buffer):
            return
        self._buffer = self._buffer[: self._pos] + self._buffer[self._pos + 1 :]
        self._on_edit()

    def _kill_word(self) -> None:
        index = self._pos
        while index > 0 and self._buffer[index - 1].isspace():
            index -= 1
        while index > 0 and not self._buffer[index - 1].isspace():
            index -= 1
        self._buffer = self._buffer[:index] + self._buffer[self._pos :]
        self._pos = index
        self._on_edit()

    def _word_bounds(self) -> tuple[int, int]:
        start = self._pos
        while start > 0 and not self._buffer[start - 1].isspace():
            start -= 1
        end = self._pos
        while end < len(self._buffer) and not self._buffer[end].isspace():
            end += 1
        return start, end

    def _accept(self) -> None:
        """Tab: replace the token under the cursor with the selected suggestion."""
        if not self._menu_open():
            return
        text = self._sugg[self._sel][0]
        if not text:
            return
        start, end = self._word_bounds()
        self._buffer = self._buffer[:start] + text + self._buffer[end:]
        self._pos = start + len(text)
        self._hist_pos = None
        self._draft = ""
        self._refresh()
        self._dismissed = True  # menu stays closed until the buffer changes again

    def _accept_pending(self) -> bool:
        """Enter/Right: take the highlighted hint when the token is not final.

        Returns ``False`` when the caller must keep its default behaviour (send
        the line, or step the cursor): the menu is closed, or the token under
        the cursor already equals the highlighted entry.  Otherwise the token
        is replaced and the suggestions are re-queried for the new text, so the
        menu can cascade (``/thinking`` -> effort levels) and the next Enter
        sends the line once the token is an exact match.
        """
        if not self._menu_open():
            return False
        text = self._sugg[self._sel][0]
        if not text:
            return False
        start, end = self._word_bounds()
        if self._buffer[start:end] == text:
            return False  # exact match: nothing left to accept, send the line
        self._accept()
        # ``_accept`` closes the menu; keep it open only while the inserted
        # token can be selected again.  A token containing whitespace cannot
        # (the cursor token ends at the space), and leaving the menu open there
        # would make repeated Enter keep accepting forever.
        token_start, token_end = self._word_bounds()
        self._dismissed = self._buffer[token_start:token_end] != text
        return True

    def _navigate(self, step: int) -> None:
        """Arrows drive the menu while it is open, otherwise the history."""
        if self._menu_open():
            self._sel = (self._sel + step) % len(self._sugg)
            return
        self._history_step(step)

    def _history_step(self, step: int) -> None:
        if not self.history:
            return
        if self._hist_pos is None:
            if step > 0:
                return
            self._draft = self._buffer
            self._hist_pos = len(self.history) - 1
        else:
            target = self._hist_pos + step
            if target < 0:
                target = 0
            if target >= len(self.history):
                self._hist_pos = None
                self._buffer = self._draft
                self._pos = len(self._buffer)
                self._on_edit(reset_history=False)
                return
            self._hist_pos = target
        self._buffer = self.history[self._hist_pos]
        self._pos = len(self._buffer)
        self._on_edit(reset_history=False)

    # --------------------------------------------------------------- history

    def _load_history(self) -> None:
        if not self.history_path:
            return
        try:
            with open(self.history_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return
        for line in lines:
            line = line.rstrip("\r")
            if not line.strip():
                continue
            if self.history and self.history[-1] == line:
                continue
            self.history.append(line)
        del self.history[:-MAX_HISTORY]

    def _remember(self, line: str) -> None:
        if not line.strip():
            return
        if self.history and self.history[-1] == line:
            return
        self.history.append(line)
        del self.history[:-MAX_HISTORY]
        if not self.history_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.history_path)), exist_ok=True)
            with open(self.history_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(self.history) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ input

    def _read_key(self) -> tuple[str, str]:
        """Return ('text', char) or ('key', name)."""
        try:
            first = os.read(self._fd, 1)
        except OSError as error:  # a pty whose master went away reports EIO
            raise EOFError("stdin closed") from error
        if not first:
            raise EOFError("stdin closed")
        byte = first[0]
        if byte == 0x03:
            raise KeyboardInterrupt
        if byte == 0x04:
            return ("key", "ctrl-d")
        if byte in (0x0D, 0x0A):
            return ("key", "enter")
        if byte in (0x7F, 0x08):
            return ("key", "backspace")
        if byte == 0x09:
            return ("key", "tab")
        if byte == 0x1B:
            return self._read_escape()
        if byte < 0x20:
            return ("key", CONTROLS.get(byte, f"ctrl-{chr(byte + 96)}"))
        if byte < 0x80:
            return ("text", chr(byte))
        extra = 1 if byte < 0xE0 else (2 if byte < 0xF0 else 3)
        data = first
        for _ in range(extra):
            if not select.select([self._fd], [], [], 0.1)[0]:
                break
            more = os.read(self._fd, 1)
            if not more:
                break
            data += more
        return ("text", data.decode("utf-8", errors="replace"))

    def _read_escape(self) -> tuple[str, str]:
        """Parse the bytes after ESC: a key sequence, or a lone ESC."""
        rest = ""
        timeout = 0.05
        while len(rest) < 8:
            if not select.select([self._fd], [], [], timeout)[0]:
                break
            chunk = os.read(self._fd, 1)
            if not chunk:
                break
            rest += chunk.decode("latin-1")
            if rest[0] not in ("[", "O"):
                break  # ESC plus a plain character (Alt-key): not a sequence
            if len(rest) > 1 and (rest[-1].isalpha() or rest[-1] in "~@"):
                break  # the introducer alone is not the end of a sequence
            timeout = 0.02
        if not rest:
            return ("key", "esc")
        if rest[0] in ("[", "O"):
            sequence = rest[1:]
            if sequence in SEQUENCES:
                return ("key", SEQUENCES[sequence])
            if sequence and sequence[-1] in SEQUENCES:
                return ("key", SEQUENCES[sequence[-1]])  # e.g. \x1b[1;5D
        return ("key", "unknown")


# --------------------------------------------------------------------- demo

DEMO_COMMANDS: list[tuple[str, str]] = [
    ("/help", "list commands"),
    ("/new", "start a new session"),
    ("/sessions", "list recent sessions"),
    ("/resume", "resume a session by name"),
    ("/session", "show the current session id"),
    ("/theme", "switch colour theme"),
    ("/themes", "list colour themes"),
    ("/exit", "leave the chat"),
]
DEMO_WORDS: list[tuple[str, str]] = [
    ("hello", "greeting"),
    ("help", "ask for help"),
    ("history", "browse the input history"),
    ("summary", "summarise this session"),
]
DEMO_COLOURS = {
    "dim": "\033[38;5;240m",
    "accent": "\033[38;5;176m",
    "ok": "\033[38;5;150m",
    "error": "\033[38;5;203m",
    "label": "\033[38;5;245m",
}


def _demo_paint(text: str, key: str) -> str:
    code = DEMO_COLOURS.get(key, "")
    return f"{code}{text}{RESET}" if code else text


def _demo_suggestions(text: str) -> list[tuple[str, str]]:
    token = text.split(" ")[-1]
    if token.startswith("/"):
        return [(name, desc) for name, desc in DEMO_COMMANDS if name.startswith(token)]
    if token:
        return [(word, desc) for word, desc in DEMO_WORDS if word.startswith(token)]
    return list(DEMO_COMMANDS)


def main() -> int:
    if not AVAILABLE:
        print("(stdin/stdout is not a TTY: falling back to plain input())", file=sys.stderr)
    history_path = os.path.join(tempfile.gettempdir(), "uachat-editor-demo.history")
    editor = Editor(
        history_path=history_path,
        suggestions=_demo_suggestions,
        paint=_demo_paint,
        max_rows=5,
    )
    print("editor demo: Tab accepts, ↑/↓ navigate, Esc closes, /exit quits")
    while True:
        try:
            line = editor.read_line(_demo_paint("› ", "accent"))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print(_demo_paint("^C", "dim"))
            continue
        if not line.strip():
            continue
        print("   " + _demo_paint("→", "ok") + " " + line)
        if line.strip() in ("/exit", "/quit"):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
