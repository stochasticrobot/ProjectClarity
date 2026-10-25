"""Drive a real terminal child process without installing a UI-testing framework.

input() and isatty() must behave as they do for a person, so these tests attach
stdin/stdout to a Linux/WSL pseudo-terminal instead of mocking terminal detection.
The driver waits for observed prompts before typing, records text for assertions,
and always closes/kills only the child it owns. All waits have short deadlines.
This module is test infrastructure, not part of the application's workflow.
"""

import os
import pty
import select
import subprocess
import time


class TerminalProcess:
    """A bounded terminal conversation with one actual Python CLI process."""

    def __init__(self, command, *, cwd, env):
        """Start one owned CLI process attached to a pseudo-terminal, preserving real input
        behavior.

        Args:
            self: Driver owning the child process, pseudo-terminal descriptors and captured
                output cursor.
            command: Argument-vector list for the child process; not a shell-command string.
            cwd: Local working directory for the owned test subprocess.
            env: Environment-variable mapping passed to the owned test subprocess.

        Returns:
            None. Spawn and retain the owned CLI subprocess and pseudo-terminal file
            descriptors.
        """
        self.master, slave = pty.openpty()
        try:
            self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env)
        finally:
            os.close(slave)
        self.output = ""
        self.cursor = 0
        self.eof = False

    def read_more(self, timeout=0.1):
        """Drain terminal output without blocking beyond the caller's deadline.

        Args:
            self: Driver owning the child process, pseudo-terminal descriptors and captured
                output cursor.
            timeout: Maximum seconds to wait for readable terminal output on this poll.

        Returns:
            None. Append available output to this driver's captured terminal buffer.
        """
        if self.eof:
            return
        if select.select([self.master], [], [], timeout)[0]:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b""  # Linux pseudo-terminals report EIO when the slave exits.
            if not data:
                self.eof = True
            self.output += data.decode("utf-8", errors="replace")

    def expect(self, text, timeout=15):
        """Observe a new occurrence of a prompt, not an old matching menu in the log.

        Args:
            self: Driver owning the child process, pseudo-terminal descriptors and captured
                output cursor.
            text: Prompt or output substring that must occur after the current consumption
                cursor.
            timeout: Maximum seconds to wait for the expected new output.

        Returns:
            None when a new occurrence of the expected text is observed; fail the test if the
            deadline expires.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            index = self.output.find(text, self.cursor)
            if index >= 0:
                self.cursor = index + len(text)
                return
            if self.eof:
                break
            self.read_more()
        raise AssertionError(f"Terminal never produced {text!r}. Last output:\n{self.output[-2500:]}")

    def send(self, text):
        """Send literal text as if typed by the user; it is never a shell command.

        Args:
            self: Driver owning the child process, pseudo-terminal descriptors and captured
                output cursor.
            text: Literal user-input text, including any intended newline; not shell code.

        Returns:
            None. Write the supplied text to the child's terminal input.
        """
        os.write(self.master, text.encode())

    def finish(self, timeout=15):
        """Drain until exit so a large final plan cannot deadlock on a full pipe.

        Args:
            self: Driver owning the child process, pseudo-terminal descriptors and captured
                output cursor.
            timeout: Maximum seconds to wait for process completion while draining output.

        Returns:
            The owned child process's exit code after its remaining terminal output is drained.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read_more()
            if self.eof:
                return self.process.wait(timeout=2)
        raise AssertionError(f"Terminal did not exit. Last output:\n{self.output[-2500:]}")

    def close(self):
        """Clean up the owned test process and PTY even after a failed assertion.

        Args:
            self: Driver owning the child process, pseudo-terminal descriptors and captured
                output cursor.

        Returns:
            None. Terminate/reap the owned child if needed and close its pseudo-terminal
            resources.
        """
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        os.close(self.master)
