import argparse
import sys

from . import APP_NAME, __version__


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qwen-chat", description=f"{APP_NAME}: local AI chat on the GPU.")
    ap.add_argument("--ask", metavar="QUESTION", help="answer one question in the terminal and exit")
    ap.add_argument("--no-web", action="store_true", help="with --ask: disable internet tools")
    ap.add_argument("--no-files", action="store_true", help="with --ask: disable workspace file tools")
    ap.add_argument("--think", action="store_true", help="with --ask: enable thinking mode")
    ap.add_argument("--yes", action="store_true",
                    help="with --ask: allow overwrites, deletes, commands and installs without asking")
    ap.add_argument("--keep-server", action="store_true", help="with --ask: leave the model loaded")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args, gtk_args = ap.parse_known_args(argv)
    if args.ask:
        from . import cli
        return cli.run(args.ask, not args.no_web, not args.no_files, args.think, args.keep_server, args.yes)
    from .app import run_app
    return run_app([sys.argv[0]] + gtk_args)


if __name__ == "__main__":
    sys.exit(main())
