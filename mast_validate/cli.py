"""``mast-validate`` command line. Exit codes: 0 clean, 1 warnings, 2 errors, 3 usage/IO."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import __version__
from .languages import TRACK_NAMES
from .report import render
from .runner import UsageProblem, validate_submission

EXIT_USAGE = 3


class _Command(click.Command):
    """Make click's own usage errors exit 3 instead of 2 (2 means validation errors here)."""

    def main(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("standalone_mode", False)
        try:
            rv = super().main(*args, **kwargs)
        except click.ClickException as exc:
            exc.show()
            sys.exit(EXIT_USAGE)
        except click.Abort:
            click.echo("Aborted!", err=True)
            sys.exit(EXIT_USAGE)
        sys.exit(rv if isinstance(rv, int) else 0)


@click.command(cls=_Command, context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--track", required=True, type=click.Choice(TRACK_NAMES, case_sensitive=False),
              help="Track the submission is for.")
@click.option("--language", "-l", default=None, metavar="LANG",
              help="Declared language of a single file (code or name). Default: from the filename.")
@click.option("--strict", is_flag=True, help="Treat warnings as errors.")
@click.option("--json", "json_path", type=click.Path(dir_okay=False, path_type=Path), metavar="PATH",
              help="Also write a machine-readable report to PATH ('-' for stdout).")
@click.option("--max-examples", default=10, show_default=True, type=click.IntRange(0, 1000),
              help="Examples shown per finding.")
@click.option("--quiet", "-q", is_flag=True, help="Summary lines only.")
@click.option("--no-color", is_flag=True, help="Plain output (for CI).")
@click.version_option(__version__, prog_name="mast-validate")
def main(path: Path, track: str, language, strict: bool, json_path, max_examples: int,
         quiet: bool, no_color: bool) -> int:
    """Validate a MAST 2026 run submission offline.

    PATH is one .jsonl (or .jsonl.gz) file, or an archive (.zip, .tar, .tar.gz,
    .tgz) holding one such file per language.

    \b
      mast-validate runs/hi.jsonl --track indic --language hi
      mast-validate submission.zip --track multilingual --json report.json
    """
    try:
        report = validate_submission(path, track=track.lower(), language=language, strict=strict,
                                     max_examples=max_examples)
    except UsageProblem as exc:
        click.echo(f"error: {exc}", err=True)
        return EXIT_USAGE
    color = (not no_color) and sys.stdout.isatty()
    text = render(report, color=color, quiet=quiet, max_examples=max_examples)
    if json_path is not None and str(json_path) == "-":
        click.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        click.echo(text, nl=False)
        if json_path is not None:
            json_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    main()
