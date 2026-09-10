import logging
import os
import shutil
import subprocess
from collections.abc import Callable

LOG = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


def command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "LC_ALL": "C",
        "LANG": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "DEBIAN_FRONTEND": "noninteractive",
    })
    return environment


class Runner:
    def available(self, executable: str) -> bool:
        return shutil.which(
            executable, path="/usr/sbin:/usr/bin:/sbin:/bin"
        ) is not None

    def executable(self, name: str) -> str:
        result = shutil.which(
            name, path="/usr/sbin:/usr/bin:/sbin:/bin"
        )
        if not result:
            raise CommandError(f"{name} is not installed.")
        return result

    def run(
        self,
        argv: list[str],
        timeout: int = 90,
        accepted: tuple[int, ...] = (0,),
    ) -> str:
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout,
                env=command_environment(),
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CommandError(str(error)) from error

        if completed.returncode not in accepted:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise CommandError(
                f"{os.path.basename(argv[0])} exited with "
                f"{completed.returncode}: {detail[:8000]}"
            )
        return completed.stdout

    def stream(self, argv: list[str], output: Callable[[str], None]) -> int:
        """No shell, no stdin, no unsafe cancellation during a transaction."""
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                env=command_environment(),
                shell=False,
            )
        except OSError as error:
            raise CommandError(str(error)) from error

        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                output(line.rstrip("\n"))
        return process.wait()
