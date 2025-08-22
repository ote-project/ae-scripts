"""Shared helpers for loading and generating the query relevance prompt."""

import subprocess
from pathlib import Path
from typing import Optional

_PROMPT_TEMPLATE: Optional[str] = None


def _load_query_relevance_template() -> str:
    """Load the latest query relevance prompt template from the sibling repo."""
    script_dir = Path(__file__).resolve().parent
    driver_dir = (script_dir / ".." / "concolic_driver").resolve()

    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=driver_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to pull latest prompts from concolic_driver.\n"
            f"Directory: {driver_dir}\n"
            f"Command: git pull --ff-only\n"
            f"Exit code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    prompt_path = driver_dir / "src" / "main" / "resources" / "prompts" / "query_relevance.txt"
    return prompt_path.read_text(encoding="utf-8")


def generate_query_relevance_prompt(query: str, stacktrace: list[str], refresh: bool = False) -> str:
    """Generate a prompt using the loaded query relevance template."""
    global _PROMPT_TEMPLATE
    if refresh or _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = _load_query_relevance_template()

    return _PROMPT_TEMPLATE.replace("{{QUERY}}", query).replace("{{STACKTRACE}}", "\n".join(stacktrace))
