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
* The editor redraws its own block (menu rows + wrapped input viewport) on every keystroke;
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
import json
import re
import shutil
import sys
import tempfile
import unicodedata
from typing import Callable
from terminal_ui import safe_text, terminal_size
from configio import file_lock, atomic_text

try:  # POSIX only: Windows has neither module.
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - platform dependent
    select = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

__all__ = ["AVAILABLE", "Editor", "pick"]

RESET = "\033[0m"
BOLD = "1"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
PASTE_ON = "\033[?2004h"
PASTE_OFF = "\033[?2004l"
CLEAR_TO_END = "\033[J"
ERASE_SCREEN = "\033[2J\033[H"
REVERSE = "\033[7m"
NL = "\r\n"
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_HISTORY = 500
MAX_PASTE_BYTES = 8 * 1024 * 1024
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
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
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
        footer=None,
    ) -> None:
        self.footer = footer
        self.history_path = history_path
        self.suggestions = suggestions or (lambda text: [])
        self.paint = paint or _plain
        self.max_rows = max(1, int(max_rows))

        self.history: list[str] = []
        self._load_history()

        self._notice = ""
        self._fd = -1
        self._buffer = ""
        self._pos = 0
        self._sugg: list[tuple[str, str]] = []
        self._sel = 0
        self._dismissed = False
        self._rows = 0
        self._cursor_row = 0
        self._hist_pos: int | None = None
        self._draft = ""

    # ---------------------------------------------------------------- public

    def read_line(self, prompt: str, initial_text: str = "") -> str:
        """Read one line.  Ctrl-C -> KeyboardInterrupt, Ctrl-D (empty) -> EOFError."""
        if not AVAILABLE:
            return input(prompt)
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except (OSError, ValueError, AttributeError, termios.error):
            return input(prompt)

        self._fd = fd
        self._buffer = safe_text(initial_text)
        self._notice = ""
        self._pos = len(self._buffer)
        self._sel = 0
        self._dismissed = False
        self._rows = 0
        self._cursor_row = 0
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
            sys.stdout.write(PASTE_ON + SHOW_CURSOR)
            self._render(prompt)
            while True:
                kind, value = self._read_key()
                if value == "idle" and kind == "key":
                    if terminal_size() != getattr(self, "_last_size", None):
                        self._render(prompt)
                    elif self.footer:
                        self.footer.draw(force=False)
                    continue
                if self.feed_key(kind,value):
                    completed = True
                    break
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

    def feed_key(self, kind: str, value: str) -> bool:
        """Apply one key; return True only for explicit submission."""
        if kind == "text":
            self._insert(value)
        elif value == "paste-too-large":
            self._notice = "Paste exceeds 8 MiB; draft unchanged"
        elif value == "newline":
            self._insert("\n")
        elif value == "enter":
            if not self._accept_pending():
                return True
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
            self._cursor_row = 0
            self._rows = 0
            sys.stdout.write(ERASE_SCREEN)
        return False

    # ------------------------------------------------------------- rendering

    def _layout(self, prompt: str, width: int) -> tuple[list[str], int, int]:
        """Explicit physical rows; reserve the last column to avoid autowrap."""
        signature = (self._buffer,self._pos,prompt,width)
        if signature == getattr(self,"_layout_signature",None):
            return self._layout_cached
        self._layout_signature = signature
        limit = max(1, width - 1)
        prompt = _fit(prompt, min(_visible_width(prompt), max(0, limit - 1)))
        indent = min(_visible_width(prompt), max(0, limit - 1))
        self._visual_indent = indent
        chunks = [[prompt]]
        spans = []
        start = 0
        col = indent
        cursor = (0, col)
        marks = 0
        for index, char in enumerate(self._buffer):
            step = _char_width(char)
            if char != "\n" and col + step > limit:
                spans.append((start, index, False))
                start = index
                chunks.append([" " * indent])
                col = indent
            if index == self._pos:
                cursor = (len(chunks) - 1, col)
            if char == "\n":
                spans.append((start, index, True))
                start = index + 1
                chunks.append([" " * indent])
                col = indent
                marks = 0
            else:
                marks = marks + 1 if step == 0 else 0
                # Bound visual combining-mark runs, without changing the draft.
                if marks <= 32:
                    chunks[-1].append(char)
                col += step
        if self._pos == len(self._buffer):
            cursor = (len(chunks) - 1, col)
        spans.append((start, len(self._buffer), True))
        self._row_spans = spans
        self._visual_cursor = cursor
        self._layout_cached = (["".join(parts) for parts in chunks], *cursor)
        return self._layout_cached

    def _render(self, prompt: str) -> None:
        if self.footer:
            self.footer.draw()
        size = terminal_size()
        self._last_size = size
        reserved = self.footer.reserved_rows() if self.footer else 0
        size = os.terminal_size((size.columns, max(2, size.lines - reserved)))
        menu = self._menu_lines(size.columns)[:max(0, size.lines - 3)]
        if self._notice and len(menu) < max(1, size.lines-3):
            menu.append(_fit(self.paint(self._notice, "error"), max(1, size.columns-1)))
        lines, row, column = self._layout(prompt, size.columns)
        capacity = max(1, size.lines - len(menu) - 1)
        if len(lines) > capacity and size.lines >= 4:
            capacity = max(1, capacity - 1)
            above = max(0, min(row - capacity + 1, len(lines) - capacity))
            below = max(0, len(lines) - above - capacity)
            menu.append(_fit(self.paint(f"  ↑ {above} · ↓ {below} rows · {len(self._buffer)} chars", "dim"), size.columns - 1))
        start = max(0, min(row - capacity + 1, len(lines) - capacity))
        visible = lines[start:start + capacity]
        target = len(menu) + row - start
        out = [HIDE_CURSOR]
        if self._cursor_row:
            out.append(f"\033[{self._cursor_row}A")
        out.append("\r" + CLEAR_TO_END)
        out.append(NL.join(menu + visible))
        bottom = len(menu) + len(visible) - 1
        if bottom > target:
            out.append(f"\033[{bottom - target}A")
        out.append("\r")
        if column:
            out.append(f"\033[{column}C")
        out.append(SHOW_CURSOR)
        self._rows = bottom + 1
        self._cursor_row = target
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        if self.footer:
            self.footer.draw()

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
        out = [PASTE_OFF, SHOW_CURSOR]
        if self._rows:
            if self._cursor_row:
                out.append(f"\033[{self._cursor_row}A")
            out.append("\r" + CLEAR_TO_END)
            if keep:
                size = terminal_size()
                lines, _, _ = self._layout(prompt, size.columns)
                cap = max(1, min(8, size.lines-3))
                shown = lines[:cap]
                if len(lines) > cap:
                    shown.append(self.paint(f"  … {len(lines)-cap} more rows · {len(self._buffer)} chars (full draft sent)", "dim"))
                out.append(NL.join(_fit(line, max(1,size.columns-1)) for line in shown))
            out.append(NL)
        elif keep:
            out.append(prompt + self._buffer + NL)
        self._rows = 0
        self._cursor_row = 0
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        if self.footer:
            self.footer.draw()

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
        self._notice = ""
        if reset_history:
            self._hist_pos = None
            self._draft = ""
        self._dismissed = False
        self._refresh()

    # ---------------------------------------------------------------- editing

    def _insert(self, text: str) -> None:
        text = safe_text(text)
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
        spans = getattr(self, "_row_spans", [])
        if len(spans) > 1:
            row, column = self._visual_cursor
            target = row + step
            if 0 <= target < len(spans):
                start, end, include_end = spans[target]
                current = self._visual_indent
                best = (float("inf"), start)
                for index in range(start, end + int(include_end)):
                    candidate = (abs(current-column), index)
                    best = min(best, candidate)
                    if index < len(self._buffer):
                        current += _char_width(self._buffer[index])
                self._pos = best[1]
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
        total = sum(len(item.encode("utf-8")) for item in self.history)
        try:
            with open(self.history_path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.rstrip("\r\n")
                    if line.startswith('"'):
                        try:
                            decoded = json.loads(line)
                            if isinstance(decoded, str): line = decoded
                        except ValueError:
                            pass
                    line = safe_text(line)
                    if not line.strip() or (self.history and self.history[-1] == line):
                        continue
                    self.history.append(line)
                    total += len(line.encode("utf-8"))
                    while self.history and (len(self.history) > MAX_HISTORY or total > MAX_HISTORY_BYTES):
                        total -= len(self.history.pop(0).encode("utf-8"))
        except OSError:
            pass

    def _remember(self, line: str) -> None:
        if not line.strip():
            return
        if not self.history_path and self.history and self.history[-1] == line:
            return
        self.history.append(line)
        del self.history[:-MAX_HISTORY]
        if not self.history_path:
            return
        try:
            with file_lock(self.history_path):
                # Merge concurrent clients instead of overwriting their history.
                self.history = []
                self._load_history()
                if not self.history or self.history[-1] != line:
                    self.history.append(line)
                self.history = self.history[-MAX_HISTORY:]
                total = sum(len(item.encode("utf-8")) for item in self.history)
                while self.history and total > MAX_HISTORY_BYTES:
                    total -= len(self.history.pop(0).encode("utf-8"))
                atomic_text(self.history_path, "\n".join(json.dumps(item, ensure_ascii=False) for item in self.history)+"\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ input

    def _read_key(self) -> tuple[str, str]:
        if not select.select([self._fd], [], [], 0.15)[0]:
            return ("key", "idle")
        return read_raw_key(self._fd)

    def _read_escape(self) -> tuple[str, str]:
        return read_raw_escape(self._fd)


def read_raw_key(fd: int) -> tuple[str, str]:
    """Return ('text', char) or ('key', name) from a raw-mode file descriptor."""
    try:
        first = os.read(fd, 1)
    except OSError as error:
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
        return read_raw_escape(fd)
    if byte < 0x20:
        return ("key", CONTROLS.get(byte, f"ctrl-{chr(byte + 96)}"))
    if byte < 0x80:
        return ("text", chr(byte))
    extra = 1 if byte < 0xE0 else (2 if byte < 0xF0 else 3)
    data = first
    for _ in range(extra):
        if not select.select([fd], [], [], 0.1)[0]:
            break
        more = os.read(fd, 1)
        if not more:
            break
        data += more
    return ("text", data.decode("utf-8", errors="replace"))


def read_raw_escape(fd: int) -> tuple[str, str]:
    """Parse the bytes after ESC: a key sequence, or a lone ESC."""
    rest = ""
    timeout = 0.05
    while len(rest) < 32:
        if not select.select([fd], [], [], timeout)[0]:
            break
        chunk = os.read(fd, 1)
        if not chunk:
            break
        rest += chunk.decode("latin-1")
        if rest[0] not in ("[", "O"):
            break
        if len(rest) > 1 and (rest[-1].isalpha() or rest[-1] in "~@"):
            break
        timeout = 0.02
    if not rest:
        return ("key", "esc")
    if rest in ("\r", "\n", "[13;2u", "[27;2;13~"):
        return ("key", "newline")
    if rest == "[200~":
        data = bytearray()
        tail = bytearray()
        oversized = False
        end = b"\x1b[201~"
        while not tail.endswith(end):
            chunk = os.read(fd, 1)
            if not chunk:
                raise EOFError("stdin closed during paste")
            if chunk == b"\x03":
                raise KeyboardInterrupt
            tail.extend(chunk)
            del tail[:-len(end)]
            if not oversized:
                data.extend(chunk)
                oversized = len(data) > MAX_PASTE_BYTES + len(end)
        if oversized:
            return ("key", "paste-too-large")
        text = data[:-len(end)].decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Never interpret pasted control bytes as terminal commands.
        text = "".join(c if c != "\t" else "    " for c in text
                       if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
        return ("text", text)
    if rest[0] in ("[", "O"):
        sequence = rest[1:]
        if sequence in SEQUENCES:
            return ("key", SEQUENCES[sequence])
        if sequence and sequence[-1] in SEQUENCES:
            return ("key", SEQUENCES[sequence[-1]])
    return ("key", "unknown")


def pick(
    items: list[tuple[str, str]],
    title: str,
    current: str = "",
    theme_paint: Callable[..., str] | None = None,
    filterable: bool = True,
    max_rows: int = 12,
) -> str | None:
    """Interactive full-featured arrow-key selector with live search filtering.

    Parameters
    ----------
    items : list of (value, description)
    title : header string displayed above the menu
    current : the currently active value (marked with [x])
    theme_paint : optional styling callable `paint(text, key, style="")`
    filterable : if True, typing characters filters the item list in real-time
    max_rows : maximum visible rows before scrolling kicks in

    Returns the selected value, or None if cancelled with Esc / Ctrl-C.
    """
    if not AVAILABLE or not items:
        return None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (OSError, ValueError, AttributeError, termios.error):
        return None

    def paint(text: str, key: str, style: str = "") -> str:
        if not theme_paint:
            return text
        try:
            return theme_paint(text, key, style)
        except TypeError:
            return theme_paint(text, key)

    query = ""
    sel = 0
    for idx, it in enumerate(items):
        if it and it[0] == current:
            sel = idx
            break
    offset = 0
    drawn = 0

    try:
        try:
            tty.setraw(fd, termios.TCSADRAIN)
        except TypeError:
            tty.setraw(fd)
        sys.stdout.write(HIDE_CURSOR)
        while True:
            cols, rows = shutil.get_terminal_size((80, 24))
            page_size = min(max_rows, max(3, rows - 5))

            if query:
                q = query.lower()
                matching = [it for it in items if q in str(it[0]).lower() or (len(it) > 1 and q in str(it[1]).lower())]
            else:
                matching = list(items)

            if not matching:
                sel = 0
                offset = 0
            else:
                sel = max(0, min(sel, len(matching) - 1))
                if sel < offset:
                    offset = sel
                elif sel >= offset + page_size:
                    offset = sel - page_size + 1

            if drawn > 0:
                sys.stdout.write(f"\033[{drawn}A\r{CLEAR_TO_END}")

            lines: list[str] = []
            hdr = paint(f"  {title}", "title", BOLD)
            if filterable:
                hdr += paint(f"  (filter: {query}█)", "accent") if query else paint("  (type to filter)", "dim")
            lines.append(hdr)
            lines.append(paint("  " + "─" * min(cols - 4, 60), "dim"))

            if not matching:
                lines.append(paint("    (no matching options)", "warn"))
            else:
                visible = matching[offset : offset + page_size]
                for idx, it in enumerate(visible):
                    real_idx = offset + idx
                    val = str(it[0])
                    desc = str(it[1]) if len(it) > 1 and it[1] else ""
                    is_sel = (real_idx == sel)
                    is_cur = (val == current)
                    ptr = "❯ " if is_sel else "  "
                    mark = "[x] " if is_cur else "    "
                    if is_sel:
                        row = REVERSE + paint(f"  {ptr}", "accent") + paint(mark, "ok") + paint(f" {val} ", "title", BOLD)
                        if desc:
                            row += paint(f"  {desc} ", "dim")
                        row += RESET
                    else:
                        row = f"  {ptr}" + paint(mark, "ok" if is_cur else "dim") + paint(val, "user")
                        if desc:
                            row += paint(f"  {desc}", "dim")
                    lines.append(_fit(row, cols - 1))

            foot = paint("  ↑/↓ move · Enter select · Esc cancel", "dim")
            if matching and len(matching) > page_size:
                foot += paint(f"  ({sel + 1}/{len(matching)})", "dim")
            lines.append(foot)

            sys.stdout.write(NL.join(lines) + NL)
            sys.stdout.flush()
            drawn = len(lines)

            kind, value = read_raw_key(fd)
            if value == "enter":
                if matching:
                    return matching[sel][0]
                return None
            if value in ("esc", "ctrl-c"):
                return None
            if value == "up":
                if matching:
                    sel = (sel - 1) % len(matching)
            elif value == "down":
                if matching:
                    sel = (sel + 1) % len(matching)
            elif value == "pageup":
                if matching:
                    sel = max(0, sel - page_size)
            elif value == "pagedown":
                if matching:
                    sel = min(len(matching) - 1, sel + page_size)
            elif value in ("home", "ctrl-a"):
                sel = 0
            elif value in ("end", "ctrl-e"):
                if matching:
                    sel = len(matching) - 1
            elif filterable and value == "backspace":
                if query:
                    query = query[:-1]
                    sel = 0
                    offset = 0
            elif filterable and value == "ctrl-u":
                if query:
                    query = ""
                    sel = 0
                    offset = 0
            elif filterable and kind == "text":
                if value.isprintable():
                    query += value
                    sel = 0
                    offset = 0
    finally:
        if drawn > 0:
            sys.stdout.write(f"\033[{drawn}A\r{CLEAR_TO_END}")
            sys.stdout.flush()
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (OSError, termios.error):
            pass


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
